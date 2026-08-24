"""从 acceptance 反推可执行的 check，并**在派发前实测过**才留下。

为什么不做进 extract.py：那边是 `--tools ""` 的纯转写，理由写在它的模块
docstring 里 —— 抽取器一旦能读仓库就会往 declared_paths 里填「看起来该改的
文件」，而那是分级引擎的输入。这里要产的是**能在这个仓库里跑起来的命令**，
不看仓库根本写不出来。两件事对仓库可见性的要求正好相反，所以分成两步：
declared_paths 仍由看不见仓库的模型产出，checks 由看得见的产出。

即便如此也不给它工具，只递一份确定性的仓库摘要（repo_digest）。递摘要而不
开工具有两个实际好处：输入可复现（同一个仓库同一份摘要），以及一次普通的
模型调用比一次 agent 跑便宜一个量级。

真正的防线不是提示词，是探针：

  **每条候选 check 都在「活还没干」的仓库里先跑一遍。现在就通过的，扔掉。**

一条在改动之前就绿的 check 证明不了改动做成了 —— 它可能在测别的东西，也
可能压根是空断言（`test -d .`、`python -c "pass"`、`true`）。红-绿这条规矩
在这里不是靠人自觉，是靠 subprocess 的返回码，零模型成本。

反过来，「现在是红的」也分两种，必须分开：
  - 功能还没写 → 正是要的（fail_missing）
  - 命令本身是坏的（127 找不到命令、126 不可执行、shell 语法错） → 扔掉。
    这种 check 改完还是红的，进队就是三轮白烧再上人，而报出来的原因
    还只是「命令退出码非 0」，人得自己去认那是拼写错。

没有 workspace 就不提议。未经探针的 check 恰好是这个模块要防的东西，
「拿不到仓库就退回猜」等于把防线让掉。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from factory.harness.checkenv import check_env
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded

_MAX_FIELD = 4000
_PROBE_TIMEOUT_S = 60

# 探针里代表「命令本身是坏的」而不是「功能还没写」的退出码。
# 127 = command not found，126 = 找到了但不可执行，2 = 多数 shell 的语法错。
# 2 有歧义（pytest 收集失败也用 2），所以它只在 stderr 也像 shell 报错时才算坏，
# 判定见 _looks_broken。
_BROKEN_EXITS = (126, 127)

_BROKEN_STDERR = (
    "command not found",
    "not found",
    "no such file or directory",
    "syntax error",
    "unexpected end of file",
    "permission denied",
)

CHECKS_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "command": {"type": "string"},
                    "expect": {
                        "type": "string",
                        "enum": ["exit_zero", "stdout_contains"],
                    },
                    "value": {"type": "string"},
                    "covers": {"type": "string"},
                },
                "required": ["name", "command", "covers"],
            },
        },
        "uncheckable": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["checks"],
}


class CheckGenError(RuntimeError):
    """提议失败。不返回半套 check —— 没探过的 check 比没有更糟。"""


_SYSTEM = """\
给定一条任务的验收标准，写出能在这个仓库里直接跑的验收命令。

你看不到仓库内容，只有下面这份摘要。**不要**猜摘要里没有的东西：
没看到测试框架就别写 pytest，没看到 package.json 就别写 npm test。

每条 check 必须：
- 在「功能还没实现」的仓库里**失败**。一条现在就能通过的命令等于没验收 ——
  它会被自动丢掉，你白写。
- 只测 covers 指的那一条 acceptance，别把多条塞进一个命令。
- 用摘要里出现过的路径和工具。模块导入路径按摘要里的目录结构写。
- 是**一条**命令。要串多步用 && 连，不要写多行脚本。

expect 取值：
- exit_zero        命令退出码 0 即通过（默认，绝大多数情况用这个）
- stdout_contains  stdout 含 value 才通过；用于命令总是退出 0 但要看输出的场合

字段：
- name     短标识，如 slugify-basic
- command  真能跑的一条命令
- expect   见上
- value    仅 stdout_contains 时填
- covers   这条 check 对应哪条 acceptance，抄原文

写不出可执行命令的 acceptance，原文放进 uncheckable，**不要**为了凑数写
`test -f xxx.py` 这种只看文件在不在的假验收 —— 文件存在证明不了行为对。
宁可少写几条。每条 check 都会被真的跑一遍，编的命令当场就露。
"""


def repo_digest(workspace: Path, *, max_entries: int = 60) -> str:
    """给模型的仓库摘要。确定性、不含文件内容。

    只列**目录结构和构建配置的存在性**，不递文件内容：写验收命令需要知道
    「测试放哪、用什么跑」，不需要知道任何一个函数长什么样。少递一样东西，
    就少一条它把仓库里现成代码抄进 command 的路。
    """
    lines: list[str] = []
    root = Path(workspace)

    markers = (
        "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini",
        "package.json", "Makefile", "justfile", "Cargo.toml", "go.mod",
    )
    present = [m for m in markers if (root / m).is_file()]
    lines.append("构建 / 测试配置：" + (", ".join(present) or "（没有）"))

    if (root / "pyproject.toml").is_file():
        try:
            text = (root / "pyproject.toml").read_text(encoding="utf-8")[:_MAX_FIELD]
            for key in ("[tool.pytest", "testpaths", "[tool.ruff", "requires-python"):
                for line in text.splitlines():
                    if line.strip().startswith(key):
                        lines.append(f"pyproject: {line.strip()[:120]}")
                        break
        except OSError:
            pass

    entries: list[str] = []
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".factory-worktrees"}
    for path in sorted(root.rglob("*")):
        if len(entries) >= max_entries:
            entries.append("…（截断）")
            break
        rel = path.relative_to(root)
        if any(part in skip or part.startswith(".") for part in rel.parts):
            continue
        entries.append(f"{rel}/" if path.is_dir() else str(rel))

    lines.append("\n仓库文件（截断到 %d 条）：" % max_entries)
    lines.extend(entries or ["（空仓库）"])
    return "\n".join(lines)


@dataclass(frozen=True)
class Probe:
    """一条候选 check 在「活还没干」的仓库里跑出来的结果。

    verdict 三种，对应三种处置：
      fail_missing  现在红的，且命令本身没坏 → 留下。这是唯一被采纳的。
      already_green 现在就绿 → 扔掉。它证明不了改动做成了。
      broken        命令本身坏了（找不到、语法错） → 扔掉，改完还是红的。
    """

    check: dict
    verdict: str
    exit_code: int | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.verdict == "fail_missing"


def _looks_broken(code: int, stderr: str, stdout: str) -> bool:
    """区分「功能没写」和「命令是坏的」。

    先看退出码里没有歧义的那几个，再退回看 stderr 的措辞。不用 `code == 2`
    单独判：pytest 收集失败也是 2，而收集失败常常正是「功能还没写」——
    误判成 broken 会把好 check 扔掉。
    """
    if code in _BROKEN_EXITS:
        return True
    blob = f"{stderr}\n{stdout}".lower()
    return any(sig in blob for sig in _BROKEN_STDERR)


def probe_check(check: dict, workspace: Path, *, timeout_s: int = _PROBE_TIMEOUT_S) -> Probe:
    """在**未改动**的仓库里跑一条候选 check。

    跑真命令、不做静态分析：一条 `python -c "pass"` 静态看不出问题，
    跑一下立刻发现它退出 0 —— 而退出 0 就是「这条 check 什么都没验」。
    """
    command = str(check.get("command", "")).strip()
    if not command:
        return Probe(check, "broken", None, "command 为空")

    try:
        # 探针跑的是模型刚提议的、没人审过的 shell。超时杀整组，
        # 别让一条乱写的 check 在机器上留东西。见 proc 模块。
        #
        # env=check_env()：不继承工厂进程的环境（H-3）。这里跑的是模型
        # 刚生成、**一个字都没人看过**的 shell —— 继承等于把 ~/.aws、
        # ANTHROPIC_API_KEY 之类交给它。实测 `echo $FAKE_API_KEY` 能读到值。
        proc = run_bounded(command, cwd=workspace, timeout_s=timeout_s,
                           shell=True, env=check_env())
    except ProcTimeout:
        # 超时的 check 在回归监工那里也会超时。不留。
        return Probe(check, "broken", None, f"探针超时（{timeout_s}s）")
    except OSError as exc:
        return Probe(check, "broken", None, f"无法执行：{exc}")

    out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
    expect = str(check.get("expect") or "exit_zero")

    if expect == "stdout_contains":
        passing = str(check.get("value", "")) in out
    else:
        passing = code == 0

    if passing:
        return Probe(check, "already_green", code,
                     "功能还没实现就已经通过了 —— 这条 check 验的不是这件事")
    if _looks_broken(code, err, out):
        return Probe(check, "broken", code, (err or out).strip()[:300])
    return Probe(check, "fail_missing", code, (err or out).strip()[:300])


@dataclass(frozen=True)
class Proposal:
    """一次提议的全部结果，含被扔掉的和为什么。

    扔掉的也留着：草稿会带着它们落盘，人打开就知道模型试过什么、
    哪条因为什么没留下。只报留下的会让「为什么只有一条 check」变成一次排查。
    """

    checks: tuple[dict, ...] = ()
    rejected: tuple[Probe, ...] = ()
    uncheckable: tuple[str, ...] = ()
    tokens: int = 0
    cost_usd: float = 0.0

    def lines(self) -> tuple[str, ...]:
        out = [f"提议 {len(self.checks) + len(self.rejected)} 条，"
               f"探针留下 {len(self.checks)} 条"]
        for p in self.rejected:
            name = p.check.get("name") or p.check.get("command", "")[:40]
            out.append(f"  ✗ {name}：{p.verdict} —— {p.detail[:120]}")
        for u in self.uncheckable:
            out.append(f"  ○ 无法机器验收：{u}")
        return tuple(out)


class CheckProposer:
    """acceptance + 仓库摘要 → 探过针的 checks。"""

    def __init__(
        self,
        *,
        binary: str = "claude",
        model: str = "sonnet",
        timeout_s: int = 300,
        probe_timeout_s: int = _PROBE_TIMEOUT_S,
    ) -> None:
        self._binary = binary
        self._model = model
        self._timeout_s = timeout_s
        self._probe_timeout_s = probe_timeout_s

    def _argv(self, prompt: str) -> list[str]:
        return [
            self._binary, "-p", prompt,
            "--model", self._model,
            "--output-format", "json",
            # 和抽取器 / 监工同一套：无工具、干净上下文。这里的无工具是为了
            # 让输入等于递进去的那份摘要 —— 能读仓库的话摘要就不是输入边界了。
            "--tools", "",
            "--safe-mode",
            "--exclude-dynamic-system-prompt-sections",
            "--json-schema", json.dumps(CHECKS_SCHEMA),
        ]

    def propose(
        self, *, acceptance, prompt: str, workspace: Path
    ) -> Proposal:
        """提议 checks 并逐条探针。workspace 必须是**活还没干**的仓库。

        acceptance 为空直接返回空 —— 没有验收标准时无从反推，
        而这种草稿闸门本来就会拦（reasons 里那条「没有可核对的验收标准」）。
        """
        criteria = tuple(str(a).strip() for a in acceptance if str(a).strip())
        if not criteria:
            return Proposal()

        ws = Path(workspace)
        if not ws.is_dir():
            raise CheckGenError(f"仓库路径不存在：{ws} —— 没有仓库就没法探针")

        body = "\n".join(f"- {c}" for c in criteria)
        full = (
            f"{_SYSTEM}\n\n---\n任务：\n{prompt[:_MAX_FIELD]}\n\n"
            f"验收标准：\n{body}\n\n---\n仓库摘要：\n{repo_digest(ws)}\n"
        )
        call = self._ask(full)
        raw = call.get("checks") or []

        probes = [
            probe_check(c, ws, timeout_s=self._probe_timeout_s)
            for c in raw
            if isinstance(c, dict) and c.get("command")
        ]
        keep = tuple(
            {k: v for k, v in {
                "name": str(p.check.get("name", "")).strip() or "check",
                "command": str(p.check["command"]).strip(),
                "expect": p.check.get("expect"),
                "value": p.check.get("value"),
            }.items() if v}
            for p in probes if p.usable
        )
        return Proposal(
            checks=keep,
            rejected=tuple(p for p in probes if not p.usable),
            uncheckable=tuple(str(u) for u in (call.get("uncheckable") or [])),
            tokens=call.get("_tokens", 0),
            cost_usd=call.get("_cost", 0.0),
        )

    def _ask(self, prompt: str) -> dict:
        """一次模型调用。失败就抛 —— 拿不到提议和「提议了 0 条」不是一回事。

        退出码不看，只读 is_error（和 harness adapter、ClaudeJudge 同源）。
        """
        with tempfile.TemporaryDirectory(prefix="factory-checkgen-") as cwd:
            try:
                proc = run_bounded(
                    self._argv(prompt), cwd=cwd, timeout_s=self._timeout_s,
                )
            except ProcTimeout:
                raise CheckGenError(f"提议 check 超时（{self._timeout_s}s）")
            except OSError as exc:
                raise CheckGenError(f"无法启动 {self._binary}：{exc}")

        try:
            payload = json.loads(proc.stdout or proc.stderr or "")
        except json.JSONDecodeError:
            raise CheckGenError(
                f"模型返回非 JSON：{(proc.stdout or proc.stderr or '')[:300]}"
            )
        if payload.get("is_error"):
            detail = " ".join(str(payload.get(k, "")) for k in
                              ("subtype", "stop_reason", "result")).strip()
            raise CheckGenError(f"模型调用失败：{detail[:300]}")

        out = payload.get("structured_output")
        if not isinstance(out, dict):
            raise CheckGenError(f"模型未返回 checks：{str(out)[:300]}")

        usage = payload.get("usage") or {}
        out["_tokens"] = int(usage.get("input_tokens", 0)) + int(
            usage.get("output_tokens", 0))
        out["_cost"] = float(payload.get("total_cost_usd") or 0.0)
        return out
