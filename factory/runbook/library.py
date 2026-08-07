"""可执行的分层 runbook 规则库。spec §8。

把 runbook 从静态 markdown 变成回归监工能真的跑的检查表。两个原来的毛病：
知识不复利（驭流踩的坑靠手工搬到 smy），以及写了不保证执行（runbook 里写了
验证步骤，但没机制保证 agent 真跑了、真看了结果）。

**一条不许放宽的边界：规则库永不从目标仓库里读。**
worker 对 workspace 有写权限（沙箱只挡 workspace *外面*），已实测确认它能
自己造出一个 `.factory/runbook.yaml`。如果规则库从 workspace 加载，worker
就能编写自己的验收标准 —— 那不是「检查表」，那是让被考的人出卷子。
所以 `load()` 显式拒绝落在 workspace 内的规则文件，而不是靠约定。

分层与覆盖：全局规则（跟工厂一起发布）→ 项目规则（工厂外的文件）。
同名时项目胜出，这就是 spec §8 说的「继承已有规则 + 声明差异」——
差异靠同名覆盖表达，不靠复制粘贴整份规则。
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import yaml

from factory.task import CheckSpec

GLOBAL_RULES = Path(__file__).with_name("global_rules.yaml")


class RunbookError(RuntimeError):
    """规则库本身有问题 —— 配置错误，不是任务失败。"""


@dataclass(frozen=True)
class RunbookRule:
    """一条可执行规则。

    when     命中哪些改动路径才启用。**空 = 永远启用** —— 所以全局规则里留空
             等于宣称「这条在所有项目所有任务上都成立」，写之前想清楚。
    requires 前置探针命令；退出非 0 则整条规则跳过（不是失败）。
             没有它的话，「本机没装 docker」会变成一条打回 worker 的 claim，
             而 worker 修不了这个 —— 白烧一轮。
    why      这条坑是怎么来的。规则库的复利全在这个字段上：没有 why，
             后人只会看到一条不知为何存在的命令，然后删掉它。
    """

    name: str
    command: str
    expect: str = "exit_zero"
    value: str = ""
    timeout_s: int = 300
    when: tuple[str, ...] = ()
    requires: str = ""
    why: str = ""
    source: str = "global"

    def applies_to(self, changed_paths: tuple[str, ...]) -> bool:
        return bool(self.matched(changed_paths))

    def matched(self, changed_paths: tuple[str, ...]) -> tuple[str, ...]:
        """本次改动里被这条规则 when 命中的文件。when 为空时是全部改动文件。"""
        if not self.when:
            return changed_paths
        return tuple(
            p for p in changed_paths if any(fnmatch(p, pat) for pat in self.when)
        )

    def to_check(self, files: tuple[str, ...] = ()) -> CheckSpec:
        """展开 `{files}` 占位符 —— 规则只查本次改动的文件，不扫整棵树。

        为什么必须这样：全树扫描下，仓库里任何一处既有违规都会打回 worker，
        而它压根没动过那些文件。实测在本工厂自己的仓库上就炸了两次
        （`.venv` 里的第三方代码、规则文件匹配到自己写的模式）。
        那样的规则库在任何存量项目上都没法开，等于白做。

        `{files}` 走 shlex.quote：路径来自 diff，也就是来自 worker。
        一个叫 `a.py; rm -rf ~` 的文件名会把后面的内容当命令执行。
        规则命令本身是我们写的（可信），插进去的路径不是。
        """
        command = self.command
        if "{files}" in command:
            # 没有命中文件时给 /dev/null，命令仍然合法且必然通过 ——
            # 空展开会让 `grep pat` 变成读 stdin，直接挂住。
            quoted = " ".join(shlex.quote(f) for f in files) or "/dev/null"
            command = command.replace("{files}", quoted)
        # 名字带上来源前缀：监工打回时 claim 里能看出这条是继承来的还是任务自带的。
        return CheckSpec(
            name=f"{self.source}:{self.name}",
            command=command,
            expect=self.expect,
            value=self.value,
            timeout_s=self.timeout_s,
        )


@dataclass(frozen=True)
class Selection:
    """选出来的检查 + 被跳过的规则。

    skipped 必须往外传，不能内部吞掉：静默跳过和「审计悄悄少东西」是同一类
    故障 —— 全绿看起来一模一样，但其实少查了几项。
    """

    checks: tuple[CheckSpec, ...] = ()
    skipped: tuple[tuple[str, str], ...] = field(default=())  # (规则名, 原因)


class RunbookLibrary:
    """分层规则集合。全局在前，项目在后（同名时后者覆盖）。"""

    def __init__(self, rules: tuple[RunbookRule, ...]) -> None:
        self._rules = rules

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def rules(self) -> tuple[RunbookRule, ...]:
        return self._rules

    @classmethod
    def load(
        cls,
        *,
        project: str | Path | None = None,
        workspace: str | Path | None = None,
        include_global: bool = True,
    ) -> RunbookLibrary:
        """加载全局 + 项目规则。

        workspace 给了就做边界检查：project 规则文件不许落在 workspace 里面。
        理由见模块 docstring —— worker 能写 workspace，让它出卷子等于没有卷子。
        这里**报错**而不是忽略：静默忽略的话，人以为项目规则生效了而实际没有。
        """
        rules: list[RunbookRule] = []
        if include_global:
            rules += _read(GLOBAL_RULES, source="global")

        if project is not None:
            path = Path(project).resolve()
            if workspace is not None:
                ws = Path(workspace).resolve()
                if path == ws or ws in path.parents:
                    raise RunbookError(
                        f"项目规则不能放在 workspace 里：{path}\n"
                        f"worker 对 workspace 有写权限，从那里读规则等于让它"
                        f"自己编写验收标准。请把规则文件放到 workspace 外面。"
                    )
            rules += _read(path, source="project")

        return cls(_dedupe(tuple(rules)))

    def select(
        self, changed_paths: tuple[str, ...], workspace: Path
    ) -> Selection:
        """挑出适用于本次改动、且前置探针通过的规则。"""
        checks: list[CheckSpec] = []
        skipped: list[tuple[str, str]] = []

        for rule in self._rules:
            files = rule.matched(changed_paths)
            if not files:
                skipped.append((rule.name, "when 不匹配本次改动"))
                continue

            if "{files}" in rule.command:
                # 只留真实存在的文件。diff 里包含被**删除**的路径，而 grep 遇到
                # 不存在的文件退出码是 2；命令前面的 `!` 会把这个错误翻成"通过"。
                # 也就是说：一次改动里只要带上一个删除的文件，整条规则就静默失效。
                # 实测踩到过 —— no-debug-leftovers 在明明有 breakpoint() 的仓库上
                # 判了 pass。静默漏查比误报更难发现，因为没有任何迹象。
                present = tuple(f for f in files if (workspace / f).is_file())
                if not present:
                    skipped.append((rule.name, "命中的文件都不存在（已删除？）"))
                    continue
                files = present

            if rule.requires and not _probe(rule.requires, workspace):
                skipped.append((rule.name, f"前置不满足：{rule.requires}"))
                continue
            checks.append(rule.to_check(files))

        return Selection(checks=tuple(checks), skipped=tuple(skipped))


def _probe(command: str, workspace: Path) -> bool:
    """前置探针。超时/异常都算不满足 —— 探针本身不该让任务失败。"""
    try:
        return subprocess.run(
            command, shell=True, cwd=workspace,
            capture_output=True, timeout=30,
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _dedupe(rules: tuple[RunbookRule, ...]) -> tuple[RunbookRule, ...]:
    """同名后者胜出，且**保持首次出现的位置**。

    位置不变是有意的：项目规则覆盖全局规则时，检查顺序不该跟着变。
    顺序一变，打回给 worker 的 claim 顺序也变，两次跑的输出没法直接对比。
    """
    order: list[str] = []
    latest: dict[str, RunbookRule] = {}
    for r in rules:
        if r.name not in latest:
            order.append(r.name)
        latest[r.name] = r
    return tuple(latest[n] for n in order)


def _read(path: Path, *, source: str) -> list[RunbookRule]:
    if not path.exists():
        raise RunbookError(f"规则文件不存在：{path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[RunbookRule] = []
    for i, r in enumerate(doc.get("rules", [])):
        try:
            out.append(
                RunbookRule(
                    name=r["name"],
                    command=r["command"],
                    expect=r.get("expect", "exit_zero"),
                    value=r.get("value", ""),
                    timeout_s=int(r.get("timeout_s", 300)),
                    when=tuple(r.get("when", ())),
                    requires=r.get("requires", ""),
                    why=r.get("why", ""),
                    source=source,
                )
            )
        except KeyError as exc:
            raise RunbookError(
                f"{path} 第 {i + 1} 条规则缺字段 {exc}（name 和 command 必填）"
            ) from exc
    return out
