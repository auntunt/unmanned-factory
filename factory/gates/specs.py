"""闸门定义表。

每道闸门是一条 `GateSpec`：从 workspace 取一份可比较的值（`probe`），
和派发前那份基线比（`verdict`），不一致就产出 (want, got) 两句人话。

`Dispatcher` 只做三件事：循环外调一次 `baseline()`、每轮调一次
`evaluate()`、拿返回的 `GateBreach` 交给 `self._blocked()`。它不需要知道
有几道闸门、分别在看什么。

## 探针函数留在 harness/workspace.py，刻意没搬过来

这里只有「哪些闸门、什么顺序、拦下时说什么」，真正跑 git 的十三个探针
（`hook_fingerprint`、`shadow_code`、`replace_refs` …）还在
`factory/harness/workspace.py`。搬过来是 580 行位移 + 12 个测试文件改
import，换来的只是一个新文件名：

  - 那些函数是**只读的 git 查询**，不含任何判据。`head_position` 回答
    「HEAD 在哪」，跟「HEAD 不该动」是两件事。前者是 workspace 的能力，
    后者才是闸门。
  - 它们有别的调用方（`capture_diff`、架构监工的 `neighbour_context`
    跟它们同文件共用 `_git`、`_CODE_SUFFIXES`、`_VENDOR_DIRS`），搬走
    就得连着搬那些私有件，或者留一层转发 —— 两种都比现在难读。
  - 每个探针的 docstring 里记着完整的攻击复现路径（实测数字、为什么
    这个读法而不是那个）。那些是「这个 git 查询怎么写才对」的知识，
    属于 workspace，不属于闸门表。

所以边界是：**workspace 提供「怎么问 git」，gates 决定「问出来算不算
违规」**。这个包只导入，不重新实现。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.harness.workspace import (
    changed_config,
    changed_hooks,
    diff_suppressed,
    git_config,
    head_position,
    hook_fingerprint,
    index_skipped,
    info_attributes,
    newly_skipped,
    replace_refs,
    runner_hooks,
    shadow_code,
    staged_gitlinks,
)

_MAX_LISTED_PATHS = 20


def _listed(paths) -> str:
    """路径列表转成给 worker 看的一行。上限见 supervisors/scope.py 的同名理由：
    worker 要的是「处理哪几个」，糊 200 行路径进 prompt 只会挤掉真正的失败项。

    这里额外排序：闸门的输入多是 frozenset，不排序的话同一次违规两轮之间的
    措辞会变，读起来像是又出了新问题。
    """
    items = sorted(paths)
    head = items[:_MAX_LISTED_PATHS]
    text = ", ".join(str(p) for p in head)
    if len(items) > _MAX_LISTED_PATHS:
        text += f" …（共 {len(items)} 个）"
    return text


def _head_desc(pos: tuple[str, str]) -> str:
    """(哈希, ref) 转成给 worker 看的一行。

    哈希截到 12 位：递进 prompt 里要的是「动过没动过」和「往哪动了」，40 位
    全写进去只是占字数。ref 为空串是 detached，明写出来 —— 「从分支变成
    detached」本身就是这道闸门要报的一种移动，只印哈希会让它读起来像没变。
    """
    commit, ref = pos
    short = commit[:12] or "（无提交）"
    return f"{short} @ {ref or 'detached HEAD'}"


@dataclass(frozen=True)
class GateBreach:
    """一道闸门拦下来的结果。字段顺序对齐 `Dispatcher._blocked` 的参数。"""

    name: str
    oracle: str
    want: str
    got: str


@dataclass(frozen=True)
class GateSpec:
    """一道闸门。

    `probe` 取当前值，`oracle` 说明「用什么量的」，`describe` 把
    (基线, 现状) 差异写成给 worker 看的那句话。

    `needs_result` 为真时 probe 需要 harness 的执行结果（改了哪些路径），
    这类闸门不能只看 workspace。

    `breached` 决定「怎样算拦」，两类判据不能混：

      差分（默认）—— `probe() != baseline`。用于合法仓库本来就可能有的东西：
        hooks、git config、.gitattributes、gitlink。判「有没有」会让装了 lint
        hook 的正常仓库恒红。
      绝对 —— `bool(probe())`。用于任何情况下都不该出现的东西：影子代码、
        新增的 conftest.py。这类闸门**不需要基线**，`baseline()` 不会为它取值。

    分不清这两类的后果是恒红或恒绿，两者一样坏 —— 恒红会被当成闸门有毛病，
    比恒绿更快被人拆掉。
    """

    name: str
    want: str
    probe: Callable[..., Any]
    describe: Callable[[Any, Any], str]
    oracle: str | Callable[[Any], str] = ""
    needs_result: bool = False
    #: None = 差分判据（!= 基线）。给了函数就用它判，(基线, 现状) -> 是否拦。
    breached: Callable[[Any, Any], bool] | None = None
    #: 绝对判据不取基线。差分判据必须取，拿不到就该炸而不是猜。
    absolute: bool = False

    def oracle_text(self, before: Any) -> str:
        return self.oracle(before) if callable(self.oracle) else self.oracle


# ---------- 闸门定义 ----------
#
# 顺序见 ORDER，不是这里的定义次序。

_HOOK_TOUCHED = GateSpec(
    name="git-hook-touched",
    oracle="sha256 of executable files in $(git rev-parse --git-common-dir)/hooks",
    want="这一轮没有改动 git hooks",
    probe=hook_fingerprint,
    # 用 changed_hooks 而不是裸 `!=`：它只报「变了的那几个」，删掉一个 hook
    # 和加一个 hook 在裸比较下都只是「集合不等」，报不出是哪个。
    breached=lambda before, now: bool(changed_hooks(before, now)),
    # 判据是指纹差异，不是「有没有 hook」：要区分「仓库本来有 hook」和
    # 「这一轮 worker 动了 hook」。修法不是给 land 加 `--no-verify` —— 那会把
    # 人自己装的 lint hook 也一起关掉（landing.py 第 27 行是刻意的）。
    #
    # 实测的后果：pre-commit 里 `echo EVIL >> a.py; git add a.py`，监工审过的
    # 是 'x\ny\n'，`git show HEAD:a.py` 是 'x\ny\nEVIL=1\n'。中间那段差额没有
    # 任何人看过。
    describe=lambda before, now: (
        f"{len(changed_hooks(before, now))} 个 hook 变了："
        f"{_listed(changed_hooks(before, now))}。hooks 目录被父仓库和所有并行 "
        f"worktree 共用，且 commit 时会执行"
    ),
)

_SHADOW_CODE = GateSpec(
    name="shadow-code",
    oracle="git ls-files --others --ignored --exclude-standard",
    want="没有被 .gitignore 挡住的新代码文件",
    # 影子代码：被 .gitignore 挡住、其它闸门都看不见的代码文件。
    # **必须在跑 check 之前拦** —— 它污染的正是那份绿：check 跑在真实文件树
    # 上，会 import 并执行这些没人审过的文件（实测执行到了）。
    #
    # 不挂进范围监工：那个监工 declared_paths 为空时 PASS，而口述来源的任务
    # 绝大多数为空。影子代码不依赖任何声明才有意义。
    probe=lambda ws: shadow_code(Path(ws)),
    absolute=True,
    describe=lambda before, now: (
        f"{len(now)} 个文件不在任何闸门视野里，但 check 会执行它们：{_listed(now)}"
    ),
)

_RUNNER_HOOK = GateSpec(
    name="runner-hook-added",
    oracle="git diff HEAD --name-only --diff-filter=A",
    want="这一轮没有新增 runner 会自动加载的配置文件",
    # runner 自己会去加载的配置文件（conftest.py 等）。同样必须在跑 check
    # 之前 —— 这类文件的作用就是改变 check 的行为。实测：一个必然失败的测试
    # + 一个清空收集的 conftest.py → `pytest -q` 退出码 0。
    #
    # 和影子代码分成两条 claim 而不是合并：worker 要做的事不一样。影子代码是
    # 「把文件挪进版本控制或删掉」，runner hook 是「别自己出卷子」。糊成一条
    # 会让打回的指令变模糊，白烧一轮。
    probe=lambda ws: runner_hooks(Path(ws)),
    absolute=True,
    describe=lambda before, now: (
        f"新增了 {len(now)} 个 runner 自动加载的文件，它们能直接改变 check 的"
        f"结果：{_listed(now)}"
    ),
)

_DIFF_SUPPRESSED = GateSpec(
    name="diff-suppressed",
    oracle="git check-attr diff（对比 --source=HEAD）",
    want="改动的源码文件在 diff 里能看到正文",
    # diff 正文被 .gitattributes 关掉的路径。放在 check 之前 —— 它伤的不是那份
    # 绿（check 照跑照红），是两个**看 diff 正文**的模型监工的输入。一行
    # `*.py -diff` 就让 `git diff HEAD` 里的 a.py 退化成「Binary files differ」，
    # 实测 `"pdb.set_trace" in diff` 变成 False，而 changed_paths 完全不受影响
    # —— 三道闸门里两道行为正常，是这个洞难被发现的原因。
    #
    # 不改成 `git diff --text` 硬穿：实测一个 5120 字节的 PNG 会摊成 28 行原始
    # 字节，真有大二进制文件时会把监工的 prompt 冲掉。所以是定点检测。
    #
    # 判据是「这一轮才被关掉」而不是「有没有被关」：本仓库有 uv.lock，一个合法
    # 的 `*.lock -diff` 会让每次改锁文件的任务都判红。基线取自
    # `git check-attr --source=HEAD`，所以不需要提前快照 —— 归在绝对判据里。
    probe=lambda ws, paths: diff_suppressed(Path(ws), tuple(paths)),
    absolute=True,
    needs_result=True,
    describe=lambda before, now: (
        f"{len(now)} 个路径这一轮被 .gitattributes 关掉了 diff，监工看不到改了"
        f"什么：{_listed(now)}"
    ),
)

_INDEX_SKIP = GateSpec(
    name="index-skip-flag",
    oracle="git ls-files -v 首字母全是 H",
    want="这一轮没有给任何路径挂上 assume-unchanged / skip-worktree",
    # 必须在跑 check **之前**：它伤的正是那份绿。实测的完整攻击 —— 可见地把
    # `add` 改成 `a - b`（一个监工看得见的错），同时把 tests/test_calc.py 改成
    # `assert True` 并挂上 --assume-unchanged：
    #   监工看到的 changed_paths = ('calc.py',)   ← 测试文件不在里面
    #   闸门 shadow/hooks/muted 全空
    #   `pytest -q` 退出码 0，"1 passed"
    #
    # 比 .gitattributes 那条更彻底：那个只让 diff 正文退化，路径还在
    # changed_paths 里；这个把路径本身抹掉，除了这一道全盲。
    #
    # 也不是金丝雀能覆盖的：金丝雀判「这一轮动了能改变裁决的文件」，而这里那个
    # 文件在 git 眼里根本没动 —— `git add -A -N` 不碰它。
    #
    # 未审代码不会因此出货：实测 `git add -A` 不 stage 这类文件，land 会以
    # 「无文件要提交」失败。这道闸门保的是那份绿的可信度，不是出货物的纯净度。
    probe=index_skipped,
    breached=lambda before, now: bool(newly_skipped(before, now)),
    describe=lambda before, now: (
        f"{len(newly_skipped(before, now))} 个路径被挂上了索引跳过标记，"
        f"git 从此不看它们的改动，而 check 跑在真实文件树上："
        f"{_listed(newly_skipped(before, now))}"
    ),
)

_CONFIG_TOUCHED = GateSpec(
    name="git-config-touched",
    oracle="git config --local --list（派发前后对比）",
    want="这一轮没有改动仓库本地 git config",
    # **必须在两道 diff 相关闸门之前**，因为它能让那两道自己失效：
    #
    #   diff.external=<脚本>      → `git diff HEAD` 返回空字符串（实测 len=0）。
    #                              而 `--name-only` 不走 external，changed_paths
    #                              照常非空 → 「空 diff」判定也不响。两个模型
    #                              监工收到的是 "(空 diff)"。
    #   core.attributesFile=<外部> → 效果同 `*.py -diff`，但 `check-attr
    #                              --source=HEAD` **也读这个设置**，基线和现状
    #                              都是「被关掉」→ 差异为空 → diff-suppressed
    #                              那道闸门被绕过（实测）。
    #
    # 判「这一轮变了」而不是查开关名单：git 的 config 键有几百个，逐个堵是猜
    # 机制。而且 config 里的东西**不出现在任何 diff 里** —— 所有闸门加上范围
    # 监工全都看不见它，只有拿基线比才看得见。
    #
    # 合法改动不会响：实测 capture_diff / land 都不写 --local（landing.py 只
    # `git config user.email` 读一次，身份是瞬时 `-c` 传的），本仓库 6 个键
    # 在一整轮里恒定。
    probe=git_config,
    breached=lambda before, now: bool(changed_config(before, now)),
    describe=lambda before, now: (
        f"{len(changed_config(before, now))} 个 config 键变了："
        f"{_listed(changed_config(before, now))}。config 不出现在任何 diff 里，"
        f"而其中若干键能直接改变监工看到的 diff"
    ),
)

_INFO_ATTRIBUTES = GateSpec(
    name="info-attributes-touched",
    oracle="sha256 of $(git rev-parse --git-common-dir)/info/attributes",
    want="这一轮没有改动 .git/info/attributes",
    # 设 attributes 一共三条路，这是第三条：
    #   tree 里的 .gitattributes  —— diff-suppressed 管
    #   core.attributesFile       —— git-config-touched 管
    #   .git/info/attributes      —— 这一条，两道都管不到
    #
    # 为什么 diff-suppressed 管不到：它的基线是 `check-attr --source=HEAD`，而
    # `--source` 只换「tree 里那份 .gitattributes」，info/attributes 属于仓库级，
    # **基线侧和现状侧都会读它** → 差异恒为空。实测写一行 `*.py -diff` 之后
    # diff_suppressed 返回 ()，而 diff 正文已经退化成「Binary files differ」。
    #
    # 危害面更大：`.git/info` 在 **common dir**，父仓库和所有并行 worktree 共用
    # 一份（实测从 worktree 写进去，主仓库的 diff 也瞎了）—— 和 hooks 同一个
    # 性质，一个 worker 能污染所有并行任务的监工。
    #
    # 判「这一轮变了」而不是「有没有这个文件」：外部仓库合法拥有它是正常的
    # （常见配法是给生成物加 `-diff`）。本仓库根本没有，基线是空串。
    probe=info_attributes,
    describe=lambda before, now: (
        ".git/info/attributes 变了。一行 `*.py -diff` 就让监工看到的 diff 正文"
        "退化成「Binary files … differ」，而这个文件不在任何 diff、任何 config、"
        "git status 里，且被所有并行 worktree 共用"
    ),
)

_HEAD_MOVED = GateSpec(
    name="head-moved",
    oracle=lambda before: f"派发前 HEAD 在 {_head_desc(before)}",
    want="worker 没有移动 HEAD（一轮之内没有合法动作会移动它）",
    # 这是 `git diff HEAD` 的**基线端**被动了手 —— 前面几道闸门盯的都是这条
    # 命令的输入，这道盯它对比的那个点。
    #
    # 实测：worker 自己 `git commit` 把后门提交掉，只留一处干净改动没提交。
    # 于是 `git diff HEAD` 变成「相对 worker 那次提交的增量」，监工看到的 paths
    # 只有 ('README.md',)，`"pdb.set_trace" in diff` 是 False，而后门在 HEAD 里。
    # 前面五道闸门全静默，git status 只报 ' A README.md'。
    #
    # **这条路和索引跳过那条的区别是它真的出货**：那条路上 `git add -A` 不
    # stage 被标记的文件，land 会以「无文件要提交」失败，未审代码进不了历史；
    # 这条路上代码已经在历史里了，land 只是往上叠一层。
    #
    # 判 (哈希, ref) 而不是只判哈希，因为只切分支那一支哈希不变：实测
    # `git checkout -b worker-side` 之后 diff 照样诚实（pdb 可见），但 land 的
    # 提交落到 `refs/heads/worker-side`，原分支看不到它 —— 人审的是原分支，
    # 货在别处。
    probe=head_position,
    describe=lambda before, now: (
        f"HEAD 这一轮被移到了 {_head_desc(now)}。`git diff HEAD` 的基线是 HEAD，"
        "移动它就把监工的视野截短成「相对 worker 那次提交的增量」，已提交的部分"
        "谁都看不见，而 land 会直接叠在上面出货"
    ),
)

_REPLACE_REFS = GateSpec(
    name="replace-refs-changed",
    oracle=lambda before: f"派发前 refs/replace/ 下有 {len(before)} 条",
    want="worker 没有增删改 refs/replace/ 下的引用",
    # 这道比前面所有闸门都更靠下：前面盯的是 `git diff HEAD` 的输入和基线端，
    # 这道盯的是 git **回答对象内容时说的话**。所有闸门都通过问 git 来看世界，
    # 让 git 撒谎就一次性绕过全部。
    #
    # 实测两种形态，八道闸门全静默、HEAD 哈希不变、status 只报干净那处：
    #   替换 blob   —— `git diff HEAD` 退化成空字符串，paths 为 ()。
    #   替换 commit —— 伪造 tree 里带 evil.py 的提交顶掉 HEAD，那个**全新文件**
    #                  不在 diff、也不在 changed_paths（不替换时两处都有它）。
    #                  这类文件的唯一视野是 `capture_diff` 里那句 `add -A -N`，
    #                  没有第二道防线（shadow_code 管的是被 gitignore 挡住的
    #                  文件，替换与否它都返回 ()，别把它当兜底）。
    #
    # 危害是伪造那份绿，不是出货：land 只提交监工审过的那一组，实测 linked
    # worktree 里真跑一遍 evil.py 没进历史。但 check 跑在真实文件树上（实测能
    # 读到它），那份绿是假的。
    #
    # 判「这一轮变了」而不是「有没有 replace ref」：外部仓库合法拥有它是正常的
    # （`--convert-graft-file` 的产物就是它）。本仓库一条都没有。
    probe=replace_refs,
    describe=lambda before, now: (
        "refs/replace/ 这一轮变了。一条 replace ref 就让 git 在「那个对象是什么」"
        "上撒谎：替换 blob 会让 diff 变成空字符串，替换 commit 连全新文件都能"
        "藏掉，而 check 照样在真实文件树上执行它。这些引用不在任何 diff、任何 "
        "config、git status 里，且被所有并行 worktree 共用"
    ),
)

_GITLINK = GateSpec(
    name="gitlink-added",
    oracle=lambda before: f"派发前有 {len(before)} 条 gitlink",
    want="worker 没有新增或修改 mode-160000 的索引条目",
    # 前面所有攻击改的都是 git 「怎么回答」，这条不改回答：它**把代码藏进一个
    # git 不展开的黑盒**。worker 在工作区里 `git init evil/`，在里面提交
    # conftest.py / payload.py，再 `git add evil`。git 如实记一条 gitlink ——
    # 索引里只有「evil → 提交哈希」，diff 和 changed_paths 都只有目录名。
    #
    # 实测十二道现有闸门全静默，同时 pytest 在 --collect-only 阶段就执行
    # evil/conftest.py（哨兵文件被写入，多次独立量测一致）。危害同 replace ref：
    # 伪造那份绿，不出货（land 只提交审过的路径集合）。
    #
    # 判「这一轮多出来」而非「有没有」：真实项目合法拥有子模块，子模块在索引里
    # 也是 mode 160000。取 (路径, 哈希) 对以覆盖「路径不变、内层再提交一版」的
    # 重新指向攻击 —— 只看路径那一版对这支是盲的。
    probe=staged_gitlinks,
    describe=lambda before, now: (
        f"这一轮新增了 {len(now - before)} 条 gitlink（mode 160000）。gitlink 在"
        "外层 diff 里只露出一个目录名，里面可以有任意多个文件 —— pytest 在 "
        "collect 阶段就会执行其中的 conftest.py，而所有现有闸门对内层代码完全"
        "盲。这类索引条目不属于任何合法的单任务改动；添加子模块应该走独立的、"
        "人工审核的流程"
    ),
)


#: 闸门的执行顺序。**这个次序是实测教训，不是排版**，别按字母排、别按
#: 「看起来更整齐」重排。三条硬约束：
#:
#:   1. git-hook-touched 最前 —— 只有它的影响范围在本 workspace 之外
#:      （.git/hooks 在 common dir，父仓库和所有并行 worktree 共用一份）。
#:   2. git-config-touched 必须在 diff-suppressed 之前 —— core.attributesFile
#:      能让后者的基线和现状都变成「已关掉」，差异恒为空，闸门被绕过（实测）。
#:   3. 污染那份绿的几道（shadow-code / runner-hook-added / index-skip-flag）
#:      都在跑 check 之前。拦下来之后再跑 check 只是在已知被污染的树上烧钱。
#:
#: 越靠后的闸门盯的东西越底层：输入 → diff 基线端 → git 自己的回答。
ORDER: tuple[GateSpec, ...] = (
    _HOOK_TOUCHED,
    _SHADOW_CODE,
    _RUNNER_HOOK,
    _CONFIG_TOUCHED,
    _DIFF_SUPPRESSED,
    _INDEX_SKIP,
    _INFO_ATTRIBUTES,
    _HEAD_MOVED,
    _REPLACE_REFS,
    _GITLINK,
)


#: 派发前取的那一份基线。key 是闸门名。
Snapshot = Mapping[str, Any]


def gate_by_name(name: str) -> GateSpec:
    for spec in ORDER:
        if spec.name == name:
            return spec
    raise KeyError(f"没有这道闸门：{name}")


def baseline(workspace: Path) -> Snapshot:
    """取所有差分闸门的基线。

    **必须在轮次循环之外调用一次。** 每轮重取实测会漏：第一轮 worker 写了
    pre-commit → 判红打回 → 第二轮开头重取基线，那个 hook 已经在里面了 →
    差异为空 → 合并，而 commit 照样被 hook 污染（rounds=2 的真跑抓到）。

    绝对判据的闸门不在返回值里 —— 它们不需要基线，取了也没人用。
    """
    ws = Path(workspace)
    return {
        spec.name: spec.probe(ws)
        for spec in ORDER
        if not spec.absolute
    }


def evaluate(
    workspace: Path,
    before: Snapshot,
    *,
    changed_paths: tuple[str, ...],
) -> GateBreach | None:
    """按 ORDER 逐道过闸门，第一道拦下的就返回，全过返回 None。

    `before` 必须是同一次派发里 `baseline()` 的返回值。缺 key 会当场
    KeyError —— 这是刻意的：拿不到「之前是什么」的时候要炸，不要猜一个默认值。
    填 `frozenset()` 会让装了合法 pre-commit 的仓库每个任务都判红，填
    `("", "")` 会让 head-moved 在每个任务上恒红，而恒红会被人当成闸门有毛病，
    比恒绿更快被拆掉。
    """
    ws = Path(workspace)
    for spec in ORDER:
        now = (
            spec.probe(ws, changed_paths)
            if spec.needs_result
            else spec.probe(ws)
        )

        if spec.absolute:
            hit = bool(now)
            base = None
        else:
            # 差分闸门缺基线就炸，不兜底。理由见 docstring。
            base = before[spec.name]
            hit = spec.breached(base, now) if spec.breached else now != base

        if hit:
            return GateBreach(
                name=spec.name,
                oracle=spec.oracle_text(base),
                want=spec.want,
                got=spec.describe(base, now),
            )
    return None
