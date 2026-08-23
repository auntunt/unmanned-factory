"""`.checks.json` 契约：让 worker 知道自己得留下判据。

## 这个模块补的洞

`ba72d46` 把投递表单简化成「只要目标，AI 自己决定验证方式」，dispatcher 侧
也接好了读取端（`_load_worktree_checks` 读 worktree 根的 `.checks.json`）。
但**没有任何一处告诉 worker 这个文件的存在**：`Dispatcher._prompt()` 第一轮
原封不动只发 `task.prompt`。

于是无 check 任务的实际轨迹（audit.db 里两条都是这个形状）：

    第 1 轮 worker 改完代码，不知道要写 .checks.json
      → _load_worktree_checks() 返回 ()
      → 回归监工 no-checks-defined FAIL
    第 2、3 轮 打回理由是「期望 '至少一条廉价客观裁判'，实得 '任务没有定义
      任何 check'」—— worker 依然不知道该往哪写，只能再猜一遍
      → 三轮烧完升级给人

`T-build-public-intelligence-mvp` 就这样花了 $4.96 / 900s 换回一句「你没写
判据」。这不是模型能力不够，是契约从没送达 —— 一个读取端等着一份没人被
要求写的文件。

## 为什么写在 prompt 里，而不是放宽监工

放宽「无 check 即 FAIL」是另一条路，但那条路把「全绿」的含义掏空了：
回归监工是四道闸门里唯一确定性、零模型调用的一道，它判 PASS 的全部依据
就是那组 check 真的跑过。没有 check 还放行，等于让工厂在「什么都没验」的
状态下 merge —— 这比多烧三轮严重得多。

所以修法是把契约补齐，让 worker 能满足这道闸门，而不是撤掉闸门。

## 判据要求写死在文本里

只说「请写 .checks.json」不够。worker 完全可能写出 `command: "echo ok"`
这种永真 check —— 那和没有 check 等价，甚至更坏（它会骗过闸门）。所以
契约文本必须同时说清「什么不算判据」，这几条禁令是从 metrics 报表里
「上人平均打回 2.00 次」倒推出来的：打回多来自判据没写对，不是 agent 不行。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from factory.task import CheckSpec

#: worker 把自己定义的检查写到 worktree 根的这个文件。
#: 名字带前导点：它是工具产物、不是项目源码，dispatcher 读完就不该进 commit。
CHECKS_FILENAME = ".checks.json"

#: 注入到 worker prompt 里的契约文本。
#:
#: 刻意用「你必须」而不是「建议」：这不是风格偏好，是这次派发能不能被判
#: 通过的硬前提。措辞软下来，worker 会把它当可选项跳过 —— 而跳过的代价是
#: 三轮全红。
CHECKS_CONTRACT = f"""\
## 必须留下机器可判定的验收判据

改完代码后，你**必须**在仓库根目录写一个 `{CHECKS_FILENAME}`，声明怎么验证
这次改动是对的。没有这个文件，这次任务会被判为不通过 —— 不管代码写得对不对。
回归监工只认能跑的命令，它不读你的说明文字。

格式：

```json
{{
  "checks": [
    {{"name": "unit", "command": "python -m pytest tests/ -q"}},
    {{"name": "imports", "command": "python -c 'import mypkg'"}},
    {{"name": "version", "command": "mytool --version",
     "expect": "stdout_contains", "value": "1.2"}}
  ]
}}
```

字段：
- `name`   这条检查的短名，失败时显示给人看
- `command` 在仓库根目录执行的 shell 命令
- `expect` 判定方式，省略即 `exit_zero`：
    - `exit_zero`       退出码为 0
    - `stdout_contains` stdout 含 `value`
    - `commands_agree`  `command` 与 `value` 两条命令的 stdout 完全一致
- `value`  `stdout_contains` / `commands_agree` 用到的第二个参数
- `timeout_s` 单条命令超时秒数，省略即 300

**判据必须能失败。** 下面这些不算判据，写了等于没写：
- `echo ok`、`true`、`exit 0` —— 永远绿，什么都没验
- `ls`、`cat file` —— 只证明文件存在，不证明它是对的
- 跑一个你这轮新加的、断言恒真的测试
- 把失败吞掉的命令（`... || true`、`pytest ... ; exit 0`）

自检一句：**如果我把这次的改动全部撤销，这条 check 会不会变红？**
答案是「不会」的，就换一条。

改了行为就验行为（跑测试、调接口、比对输出），改了构建就验构建能过。
仓库里已有测试框架就用它，没有就写一个最小的可执行验证。
"""


def contract_text() -> str:
    """给 prompt 用的契约正文。"""
    return CHECKS_CONTRACT


#: 恒为真的命令。整条命令就是这些之一时判定为空判据。
#:
#: 只收「整条就是它」，不做子串匹配：`echo` 出现在一条真命令里完全正常
#: （`echo $? && pytest`、`python -c 'print(1)' | grep -q 1`），
#: 按子串否决会把正当判据也拦掉，而误拦一切等于拦不住任何东西。
_ALWAYS_TRUE = frozenset({
    "true", ":", "exit 0", "/bin/true",
})

#: 只证明「文件在」不证明「文件对」的命令前缀。
#: `ls`、`cat`、`test -f`、`stat` 这类在目标存在时恒零。
_EXISTENCE_ONLY = ("ls", "cat", "stat", "file", "pwd", "whoami", "date", "env")

#: 把非零退出码吞掉的尾巴。带上它，前面的命令怎么红都无所谓。
#: `|| true` / `|| :` / `; true` / `; exit 0` 都是同一招。
_SWALLOWS_FAILURE = re.compile(
    r"(\|\|\s*(true|:|exit\s+0)|;\s*(true|:|exit\s+0))\s*$"
)


def _is_vacuous(spec: CheckSpec) -> bool:
    """这条 check 是否恒为真。

    判据是「撤销本次改动它会不会变红」，这里用几条保守的语法特征逼近它 ——
    真正的判定要跑两遍（改动前后各一次），那是 verdict_probe 的活，成本高。
    这一层只拦掉最省力的几种糊弄法，宁可漏也不误拦。
    """
    cmd = spec.command.strip()
    if not cmd:
        return True

    # `... || true` 之类：不管前面是什么，退出码被洗成 0。
    # exit_zero 之外的判定方式（stdout_contains）不吃这一套 —— 它比对输出，
    # 不看退出码，所以尾巴吞不掉它。
    if spec.expect == "exit_zero" and _SWALLOWS_FAILURE.search(cmd):
        return True

    # 多段命令（&&、|、;）不再往下判：`echo x && pytest` 的判定力在后半段。
    if any(sep in cmd for sep in ("&&", "|", ";")):
        return False

    if cmd in _ALWAYS_TRUE:
        return True

    head = cmd.partition(" ")[0].rsplit("/", 1)[-1]  # /bin/echo → echo

    # echo 任意内容都是退出 0。但 `echo ... ` 配 stdout_contains 时，
    # 比对的是 echo 自己的字面量，同样什么都没验。
    if head == "echo":
        return True

    if head in _EXISTENCE_ONLY:
        return True

    # `test -f x` / `[ -f x ]`：目标在就恒零。
    if head in {"test", "["}:
        return True

    return False


def vacuous_checks(checks: tuple[CheckSpec, ...]) -> tuple[CheckSpec, ...]:
    """这组 check 里恒为真的那些。

    返回空元组表示这组判据至少有一条真的能失败。**不要求每条都有判定力** ——
    一组里混一条 `ls`（确认产物落地了）配一条真跑的测试是正当写法。
    只有当**每一条**都恒为真时才算这组等于没有验收，那时全部返回。
    """
    if not checks:
        return ()
    vacuous = tuple(c for c in checks if _is_vacuous(c))
    return vacuous if len(vacuous) == len(checks) else ()


def load(workspace: Path) -> tuple[CheckSpec, ...]:
    """读 worker 写下的 `.checks.json`。

    格式错误一律返回空元组而不是抛：worker 写坏了 JSON 是「没留下判据」的
    一种，走 no-checks-defined 那条既有路径（打回并附上具体理由），比让
    整次派发以异常收场更有用 —— 后者连审计记录都不完整。
    """
    path = Path(workspace) / CHECKS_FILENAME
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("checks", []) if isinstance(data, dict) else []
        out = []
        for c in raw:
            if not isinstance(c, dict) or not c.get("command"):
                continue
            out.append(
                CheckSpec(
                    name=str(c.get("name") or "unnamed"),
                    command=str(c["command"]),
                    expect=str(c.get("expect", "exit_zero")),
                    value=str(c.get("value", "")),
                    timeout_s=int(c.get("timeout_s", 300)),
                )
            )
        return tuple(out)
    except (json.JSONDecodeError, TypeError, ValueError, OSError):
        return ()
