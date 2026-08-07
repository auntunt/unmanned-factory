"""runbook 规则库测试。spec §8。

规则是关于世界的断言，所以每条内置规则都要有**两个**样本：一个该触发、
一个不该触发。只测「该触发」的话，一条永远返回 FAIL 的规则也能全绿。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.runbook import RunbookError, RunbookLibrary, RunbookRule
from factory.supervisors.regression import run_check


def write(root: Path, name: str, body: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def verdict(rule_name: str, workspace: Path, paths: tuple[str, ...]) -> str:
    """跑一条内置规则，返回 'pass' / 'FAIL' / 'skipped'。"""
    lib = RunbookLibrary.load()
    sel = lib.select(paths, workspace)
    for check in sel.checks:
        if check.name.endswith(f":{rule_name}"):
            return "FAIL" if run_check(check, workspace) else "pass"
    return "skipped"


# ── 边界：规则库不能来自 workspace ────────────────────────────────────────

def test_project_rules_inside_workspace_are_rejected(tmp_path):
    """worker 有 workspace 写权限，从那里读规则等于让被考的人出卷子。

    已实测确认 worker 能在沙箱里造出 workspace/.factory/runbook.yaml
    （沙箱只挡 workspace 外面）。所以这条必须**报错**而不是忽略：静默忽略
    的话人以为项目规则生效了而实际没有。
    """
    ws = tmp_path / "ws"
    rules = write(ws, ".factory/runbook.yaml", "rules: []\n")
    with pytest.raises(RunbookError, match="workspace"):
        RunbookLibrary.load(project=rules, workspace=ws)


def test_project_rules_outside_workspace_are_accepted(tmp_path):
    """反向：放在 workspace 外面就正常加载。

    没有这条，上面那条无法区分"边界检查起作用了"和"项目规则压根加载不了"。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    rules = write(tmp_path / "cfg", "runbook.yaml",
                  "rules:\n  - name: r\n    command: 'true'\n")
    lib = RunbookLibrary.load(project=rules, workspace=ws, include_global=False)
    assert [r.name for r in lib.rules] == ["r"]


def test_missing_required_field_names_the_rule(tmp_path):
    rules = write(tmp_path, "r.yaml", "rules:\n  - name: no-command\n")
    with pytest.raises(RunbookError, match="第 1 条"):
        RunbookLibrary.load(project=rules, include_global=False)


# ── 分层覆盖 ──────────────────────────────────────────────────────────────

def test_project_rule_overrides_global_by_name(tmp_path):
    rules = write(
        tmp_path, "r.yaml",
        "rules:\n  - name: no-debug-leftovers\n    command: 'true'\n",
    )
    lib = RunbookLibrary.load(project=rules)
    hit = [r for r in lib.rules if r.name == "no-debug-leftovers"]
    assert len(hit) == 1, "同名规则应该只剩一条"
    assert hit[0].source == "project" and hit[0].command == "true"


def test_override_keeps_original_position(tmp_path):
    """覆盖不该改变检查顺序 —— 顺序一变，两次跑的 claim 顺序就没法直接对比。"""
    base = RunbookLibrary.load()
    idx = [r.name for r in base.rules].index("no-debug-leftovers")
    rules = write(
        tmp_path, "r.yaml",
        "rules:\n  - name: no-debug-leftovers\n    command: 'true'\n",
    )
    after = RunbookLibrary.load(project=rules)
    assert [r.name for r in after.rules].index("no-debug-leftovers") == idx


# ── 选择：只查改动的文件 ──────────────────────────────────────────────────

def test_rules_only_scan_changed_files(tmp_path):
    """存量违规不该打回一个没动过它的 worker。

    实测：全树扫描在本工厂自己的仓库上误报两次（.venv 里的第三方代码带
    `import pdb`、规则文件本身匹配到自己写的模式）。误报会无理由打回，
    烧掉一整轮，还让 P1 的「上人平均打回次数」这个指标失真。
    """
    ws = tmp_path / "ws"
    write(ws, "legacy.py", "import pdb\n")        # 既有违规，本次没动
    write(ws, "new.py", "def f():\n    return 1\n")  # 本次改动，干净
    assert verdict("no-debug-leftovers", ws, ("new.py",)) == "pass"
    # 反向：真动了那个脏文件就该红
    assert verdict("no-debug-leftovers", ws, ("legacy.py",)) == "FAIL"


def test_deleted_paths_do_not_silently_disable_a_rule(tmp_path):
    """回归：diff 里带一个被删除的文件，曾让整条规则静默失效。

    grep 遇到不存在的文件退出码是 2，而命令前的 `!` 把这个错误翻成"通过"。
    实测：明明有 breakpoint() 的仓库判了 pass。**静默漏查比误报更难发现**，
    因为它不留任何痕迹 —— 全绿看起来和真的没问题一模一样。
    """
    ws = tmp_path / "ws"
    write(ws, "dirty.py", "def g():\n    breakpoint()\n")
    # gone.py 出现在改动列表里但已不存在（worker 删掉了它）
    assert verdict("no-debug-leftovers", ws, ("gone.py", "dirty.py")) == "FAIL"


def test_rule_skipped_when_all_matched_files_are_gone(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    lib = RunbookLibrary.load()
    sel = lib.select(("deleted.py",), ws)
    assert not any(c.name.endswith(":no-debug-leftovers") for c in sel.checks)
    assert any("不存在" in why for _, why in sel.skipped)


def test_skipped_rules_are_reported_not_swallowed(tmp_path):
    """跳过必须往外传。静默跳过和"审计悄悄少东西"是同一类故障。"""
    ws = tmp_path / "ws"
    write(ws, "a.py", "x = 1\n")
    sel = RunbookLibrary.load().select(("a.py",), ws)
    names = {n for n, _ in sel.skipped}
    assert "docker-compose-no-restart-only" in names, "不适用的规则要留痕"


def test_requires_probe_skips_instead_of_failing(tmp_path):
    """前置不满足 → 跳过，不是 FAIL。worker 修不了环境，那会白烧一轮。"""
    ws = tmp_path / "ws"
    write(ws, "a.py", "x = 1\n")
    rules = write(
        tmp_path, "r.yaml",
        "rules:\n  - name: needs-missing-tool\n"
        "    command: 'false'\n"
        "    requires: 'command -v definitely-not-installed-xyz'\n"
        "    when: ['*.py']\n",
    )
    sel = RunbookLibrary.load(project=rules, include_global=False).select(
        ("a.py",), ws
    )
    assert sel.checks == ()
    assert any("前置不满足" in why for _, why in sel.skipped)


def test_filenames_from_diff_are_shell_quoted(tmp_path):
    """路径来自 worker，必须 quote —— 否则文件名就是注入点。"""
    rule = RunbookRule(name="r", command="cat {files}", when=("*.py",))
    evil = "a.py; touch /tmp/factory-runbook-injection"
    check = rule.to_check((evil,))
    assert "'" in check.command, f"未加引号：{check.command}"
    assert "; touch" not in check.command.replace(f"'{evil}'", "")


def test_when_empty_means_always(tmp_path):
    rule = RunbookRule(name="r", command="true")
    assert rule.applies_to(("anything.txt",))
    assert rule.matched(("a", "b")) == ("a", "b")


# ── 每条内置规则的两个样本 ────────────────────────────────────────────────
#
# 只测"该触发"的话，一条永远返回 FAIL 的规则也能全绿；只测"不该触发"的话，
# 一条永远 pass 的空规则也能全绿。所以每条都要两样本。

BUILTIN_CASES = [
    # (规则名, 文件名, 违规内容, 合规内容)
    (
        "docker-compose-no-restart-only",
        "deploy.sh",
        "#!/bin/sh\ndocker compose restart web\n",
        "#!/bin/sh\ndocker compose down && docker compose up -d --force-recreate\n",
    ),
    (
        "docker-buildx-needs-load",
        "build.sh",
        "docker buildx build -t app:1 .\n",
        "docker buildx build --load -t app:1 .\n",
    ),
    (
        "no-localhost-in-shipped-config",
        "docker-compose.yml",
        "services:\n  web:\n    environment:\n      API: http://localhost:8000\n",
        "services:\n  web:\n    environment:\n      API: http://api.internal:8000\n",
    ),
    (
        "python-files-compile",
        "mod.py",
        "def broken(:\n",
        "def fine():\n    return 1\n",
    ),
    (
        "no-debug-leftovers",
        "mod.py",
        "def g():\n    breakpoint()\n",
        "def g():\n    return 1\n",
    ),
]


@pytest.mark.parametrize(
    "rule_name,filename,violating,clean",
    BUILTIN_CASES,
    ids=[c[0] for c in BUILTIN_CASES],
)
def test_builtin_rule_fires_on_violation_and_not_on_clean(
    tmp_path, rule_name, filename, violating, clean
):
    bad = tmp_path / "bad"
    write(bad, filename, violating)
    assert verdict(rule_name, bad, (filename,)) == "FAIL", (
        f"{rule_name} 没有抓到违规 —— 漏报"
    )

    good = tmp_path / "good"
    write(good, filename, clean)
    assert verdict(rule_name, good, (filename,)) == "pass", (
        f"{rule_name} 把合规代码判成了违规 —— 误报，会无理由打回 worker"
    )


def test_every_builtin_rule_has_a_sample_pair():
    """新加内置规则必须同时加样本，否则这条测试会红。

    没有它的话，规则库会慢慢积累一堆没人验证过的规则 —— 而一条从没被
    两个样本检验过的规则，既可能永远不叫，也可能永远乱叫。
    """
    covered = {c[0] for c in BUILTIN_CASES}
    builtin = {r.name for r in RunbookLibrary.load().rules}
    assert builtin == covered, (
        f"缺样本的规则：{sorted(builtin - covered)}；"
        f"样本对应不上的：{sorted(covered - builtin)}"
    )


def test_every_builtin_rule_explains_why():
    """why 是这份规则库唯一的复利来源。没有它，后人会删掉看不懂的规则。"""
    missing = [r.name for r in RunbookLibrary.load().rules if len(r.why) < 40]
    assert not missing, f"这些规则没写清 why：{missing}"


# ── 接进 dispatcher：规则要真的能拦住合并 ────────────────────────────────
#
# spec §8 的第二个毛病是"写了不保证执行"。库本身全绿但没接上，等于还是没执行，
# 所以这一段测的是接线，不是库。

def _stub_result(changed: tuple[str, ...], diff: str = "diff"):
    from factory.harness.base import AttemptResult, ExitStatus

    # ok 是从 exit_status 推出来的 property，不是字段 —— 不能直接传。
    return AttemptResult(
        exit_status=ExitStatus.OK, changed_paths=changed,
        diff=diff, diff_hash="h", transcript_path=None,
        tokens_in=1, tokens_out=1, cost_usd=0.0, wall_clock_ms=1,
        harness_version="stub",
    )


class _StubAdapter:
    name = "stub"

    def __init__(self, workspace: Path, body: str):
        self._ws, self._body = workspace, body

    def version(self) -> str:
        return "stub"

    def run(self, task, workspace, limits, *, model):
        write(Path(workspace), "mod.py", self._body)
        return _stub_result(("mod.py",))


def _dispatch(tmp_path, body: str, *, runbook):
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.task import CheckSpec, Task

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    task = Task(
        task_id="T-rb", prompt="p", declared_paths=("mod.py",),
        # 任务自带的 check 永远通过：这样红只可能来自 runbook 规则
        checks=(CheckSpec(name="always-ok", command="true"),),
        max_rounds=1,
    )
    d = Dispatcher(
        adapter=_StubAdapter(ws, body),
        store=AuditStore(tmp_path / f"{abs(hash(body)) % 10**6}.db"),
        runbook=runbook,
    )
    return d.run(task, ws)


def test_runbook_rule_blocks_the_merge(tmp_path):
    from factory.dispatcher import Outcome

    report = _dispatch(
        tmp_path, "def g():\n    breakpoint()\n", runbook=RunbookLibrary.load()
    )
    assert report.outcome is not Outcome.MERGED, "规则没拦住 —— 等于没接上"
    assert "no-debug-leftovers" in report.escalation_reason


def test_same_code_merges_without_the_runbook(tmp_path):
    """反向：不开规则库时同样的代码照过。

    没有这条，上一条无法区分"规则起作用了"和"这次派发本来就会失败"。
    """
    from factory.dispatcher import Outcome

    report = _dispatch(tmp_path, "def g():\n    breakpoint()\n", runbook=None)
    assert report.outcome is Outcome.MERGED


def test_runbook_claim_names_the_rule_source(tmp_path):
    """打回给 worker 的 claim 要能看出这条是继承来的还是任务自带的。"""
    report = _dispatch(
        tmp_path, "def g():\n    breakpoint()\n", runbook=RunbookLibrary.load()
    )
    assert "global:" in report.escalation_reason
