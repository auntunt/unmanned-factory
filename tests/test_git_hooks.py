"""git hooks 这道视野差：`.git/hooks` 是**公共** git dir 的一部分。

前面三条检测（后分级、影子代码、runner hook）看的都是 workspace 里的文件。
这一条不一样 —— hooks 目录被父仓库和所有并行 worktree 共用，而 sandbox
刻意把 git_dir 放进可写（capture_diff 要写 index.lock）。于是 worker 在
自己 worktree 里写的 pre-commit 会在**别人**提交时执行。

而且它的时机比前两条晚：影子代码和 runner hook 污染的是 check 那一份绿，
hook 污染的是 **commit 的内容本身** —— 监工审完、判绿之后 land 才提交，
hook 在那一刻往文件里追加东西。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.landing import land
from factory.harness.workspace import (
    changed_hooks,
    hook_fingerprint,
    hooks_dir,
)


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "a@b.c")
    _git(root, "config", "user.name", "a")
    (root / "a.py").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _hook(root: Path, name: str, body: str) -> Path:
    d = hooks_dir(root)
    assert d is not None
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return p


# ---------- 先证明洞存在 ----------

def test_the_hole_is_real_a_hook_changes_what_gets_committed(tmp_path):
    """pre-commit 追加内容 → commit 里的东西 ≠ 监工审过的东西。

    这就是 landing 模块 docstring 里那句「监工审的是前者，出货的是后者，
    中间那段差额没有任何人看过」，只是这次差额是 hook 加的，而
    `land` 刻意不加 `--no-verify`（人自己的 lint hook 该跑）。
    """
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    _hook(root, "pre-commit", 'echo "EVIL=1" >> a.py\ngit add a.py')

    (ws / "a.py").write_text("x\ny\n")
    reviewed = (ws / "a.py").read_text()

    landed = land(ws, task_id="T-1", attempt_no=1, paths=("a.py",))
    assert landed, landed.reason
    shipped = _git(ws, "show", "HEAD:a.py").stdout
    assert reviewed != shipped
    assert "EVIL=1" in shipped


def test_the_hooks_dir_is_shared_with_the_parent_and_other_worktrees(tmp_path):
    """worker 在自己 worktree 里写的 hook 会作用到父仓库和别的 worktree。

    这是这条检测和前两条最大的差别：前两条的影响关在 workspace 里，
    这条越过了 workspace 边界，所以它排在三条检测的最前面。
    """
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "w1"), "-b", "f1")
    _git(root, "worktree", "add", "-q", str(tmp_path / "w2"), "-b", "f2")

    assert hooks_dir(tmp_path / "w1") == hooks_dir(root)
    assert hooks_dir(tmp_path / "w1") == hooks_dir(tmp_path / "w2")

    # w1 里写的 hook，w2 提交时执行
    _hook(tmp_path / "w1", "pre-commit", 'echo "EVIL=1" >> a.py\ngit add a.py')
    ws2 = tmp_path / "w2"
    (ws2 / "a.py").write_text("x\nz\n")
    assert land(ws2, task_id="T-2", attempt_no=1, paths=("a.py",))
    assert "EVIL=1" in _git(ws2, "show", "HEAD:a.py").stdout


# ---------- 指纹 ----------

def test_samples_are_not_counted(tmp_path):
    """`git init` 装的 .sample 不算 —— 每个仓库都有一堆（实测 14 个）。

    算进来的后果不是「多报几个」，而是让「有没有变」这个信号钝掉：
    基线里恒定带着 14 个条目，读报表的人会习惯性跳过这一段。
    """
    root = _repo(tmp_path)
    d = hooks_dir(root)
    assert list(d.glob("*.sample")), "这台机器的 git init 没装样例，测试前提没了"
    assert hook_fingerprint(root) == frozenset()


def test_a_non_executable_hook_is_not_counted(tmp_path):
    """不可执行的文件 git 不会跑，不算 hook。"""
    root = _repo(tmp_path)
    p = _hook(root, "pre-commit", "echo hi")
    p.chmod(0o644)
    assert hook_fingerprint(root) == frozenset()


def test_a_real_hook_is_fingerprinted(tmp_path):
    root = _repo(tmp_path)
    _hook(root, "pre-commit", "echo hi")
    fp = hook_fingerprint(root)
    assert sorted(n for n, _ in fp) == ["pre-commit"]


def test_the_fingerprint_is_stable_across_calls(tmp_path):
    """连续两次必须一致，否则这道闸门每一轮都响。"""
    root = _repo(tmp_path)
    _hook(root, "pre-commit", "echo hi")
    assert changed_hooks(hook_fingerprint(root), hook_fingerprint(root)) == ()


def test_a_repo_with_a_legitimate_hook_is_not_flagged(tmp_path):
    """仓库本来就有的 hook 不报 —— 判据是「这一轮动了没」，不是「有没有」。

    land 刻意不加 `--no-verify`（landing.py:27），也就是说人装的 lint /
    格式化 hook 本来就该跑。把「有 hook」当异常等于和那条决定打对台。
    """
    root = _repo(tmp_path)
    _hook(root, "pre-commit", "exec ruff check .")
    before = hook_fingerprint(root)
    (root / "a.py").write_text("x\ny\n")  # worker 只改代码，没动 hook
    assert changed_hooks(before, hook_fingerprint(root)) == ()


def test_content_change_is_detected(tmp_path):
    """名字没变、内容变了也要报 —— 在合法 hook 里塞一行是最省事的攻击。"""
    root = _repo(tmp_path)
    _hook(root, "pre-commit", "exec ruff check .")
    before = hook_fingerprint(root)
    _hook(root, "pre-commit", 'exec ruff check .\necho "EVIL" >> a.py')
    assert changed_hooks(before, hook_fingerprint(root)) == ("pre-commit",)


def test_deletion_is_detected(tmp_path):
    """删掉人装的 hook 也报：那等于关掉人的一道闸门。"""
    root = _repo(tmp_path)
    p = _hook(root, "pre-commit", "exec ruff check .")
    before = hook_fingerprint(root)
    p.unlink()
    assert changed_hooks(before, hook_fingerprint(root)) == ("pre-commit",)


def test_making_an_existing_sample_executable_is_detected(tmp_path):
    """把样例改名成真 hook 并 chmod +x —— 内容一个字没改，行为从不生效变生效。

    这条是滤 .sample 这个决定的另一面：滤掉的判据必须是「名字带 .sample」
    而不是「内容长这样」，否则复制一份改名就绕过了。
    """
    root = _repo(tmp_path)
    d = hooks_dir(root)
    before = hook_fingerprint(root)
    sample = d / "pre-commit.sample"
    assert sample.exists()
    live = d / "pre-commit"
    live.write_bytes(sample.read_bytes())
    live.chmod(0o755)
    assert changed_hooks(before, hook_fingerprint(root)) == ("pre-commit",)


def test_core_hookspath_is_honoured(tmp_path):
    """跟着 core.hooksPath 走，不硬拼 .git/hooks。

    改这个配置本身就是把 hooks 换掉的一种方式，如果检测只看 .git/hooks，
    换过路径之后基线和实际就都在看一个没人用的目录。
    """
    root = _repo(tmp_path)
    alt = root / "myhooks"
    alt.mkdir()
    _git(root, "config", "core.hooksPath", "myhooks")
    assert hooks_dir(root) == alt

    before = hook_fingerprint(root)
    p = alt / "pre-commit"
    p.write_text("#!/bin/sh\necho hi\n")
    p.chmod(0o755)
    assert changed_hooks(before, hook_fingerprint(root)) == ("pre-commit",)


def test_a_worktree_asks_git_instead_of_joining_dot_git(tmp_path):
    """worktree 里 `.git` 是文件不是目录，拼路径会拿到一个不存在的目录。"""
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    assert (ws / ".git").is_file()
    assert not (ws / ".git" / "hooks").exists()
    assert hooks_dir(ws) == hooks_dir(root)


def test_not_a_repo_returns_none(tmp_path):
    """不是仓库时返回 None、指纹为空，而不是抛异常。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hooks_dir(plain) is None
    assert hook_fingerprint(plain) == frozenset()


def test_this_repo_is_quiet():
    """每次都响的闸门等于没有闸门 —— 本仓库上必须是 0。"""
    assert hook_fingerprint(Path(".")) == frozenset()


# ---------- 接线 ----------

def _dispatch(tmp_path, ws, checks_ran, *, writes_hook: str | None = None):
    """跑一轮真 dispatcher。`writes_hook` 模拟 worker 在派发**期间**写 hook。

    必须在 adapter.run 里写，不能在调 _dispatch 之前写：基线是在派发前取的，
    提前写等于把 worker 干的事算进基线，那这道闸门就永远不响了。
    """
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class HookWriting(FakeAdapter):
        def run(self, task, workspace, limits, *, model=None):
            if writes_hook is not None:
                _hook(Path(workspace), "pre-commit", writes_hook)
            return super().run(task, workspace, limits, model=model)

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(task_id="T-gh", prompt="改点东西",
                checks=(CheckSpec(name="ok", command="true"),))
    d = Dispatcher(adapter=HookWriting([_result(paths=("a.py",))]),
                   store=AuditStore(tmp_path / "a.db"),
                   supervisor=Recording())
    return d.run(task, ws)


def _worktree(tmp_path) -> Path:
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    (ws / "a.py").write_text("x\ny\n")
    return ws


def test_a_worker_written_hook_blocks_the_merge(tmp_path):
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], writes_hook='echo "EVIL=1" >> a.py')
    assert rep.outcome.value != "merged", f"动了 hook 却合并了：{rep}"
    assert "pre-commit" in rep.escalation_reason


def test_nothing_is_committed_when_a_hook_was_touched(tmp_path):
    """判红就不落地，所以那个被 hook 污染的 commit 根本不存在。"""
    ws = _worktree(tmp_path)
    head_before = _git(ws, "rev-parse", "HEAD").stdout.strip()
    _dispatch(tmp_path, ws, [], writes_hook='echo "EVIL=1" >> a.py\ngit add a.py')
    assert _git(ws, "rev-parse", "HEAD").stdout.strip() == head_before


def test_the_baseline_is_taken_once_not_per_round(tmp_path):
    """基线取在循环外。取在循环里的话，第二轮就把 hook 当成「本来就有」了。

    这是接线测试抓到的真 bug，不是假想：第一版基线在 for 里，第一轮写
    hook 判红打回，第二轮重取基线 → 差异为空 → MERGED，commit 照样被
    污染。所以这条测试跑满两轮，且第二轮 worker 什么都不做。
    """
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    ws = _worktree(tmp_path)
    rounds: list[int] = []

    class WritesOnce(FakeAdapter):
        def run(self, task, workspace, limits, *, model=None):
            rounds.append(1)
            if len(rounds) == 1:
                _hook(Path(workspace), "pre-commit", 'echo "EVIL=1" >> a.py')
            return super().run(task, workspace, limits, model=model)

    class AlwaysPass:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(task_id="T-gh2", prompt="改点东西", max_rounds=2,
                checks=(CheckSpec(name="ok", command="true"),))
    rep = Dispatcher(adapter=WritesOnce([_result(paths=("a.py",))]),
                     store=AuditStore(tmp_path / "b.db"),
                     supervisor=AlwaysPass()).run(task, ws)

    assert len(rounds) == 2, f"该跑满两轮：{rounds}"
    assert rep.outcome.value != "merged", (
        f"第二轮把上一轮写的 hook 当成基线了：{rep}")


def test_checks_still_run_when_only_a_hook_was_touched(tmp_path):
    """和前两条检测的区别：hook 污染的是 commit 内容，不是那份绿。

    所以这里**不要求**拦在 check 之前 —— 只要求不合并。写死「check 没跑」
    会把一个和危害无关的实现细节焊进测试。
    """
    ws = _worktree(tmp_path)
    ran: list = []
    rep = _dispatch(tmp_path, ws, ran, writes_hook='echo hi')
    assert rep.outcome.value != "merged"


def test_the_claim_is_distinct_from_the_other_two(tmp_path):
    """三条 claim 互不相同：worker 的补救动作各不一样。

    影子代码 → 挪进版本控制或删掉；runner hook → 别自己出卷子；
    这一条 → 别动 .git/hooks。糊成一条会让打回指令变模糊，白烧一轮。
    """
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], writes_hook="echo hi")
    assert "git-hook-touched" in rep.escalation_reason
    assert "shadow-code" not in rep.escalation_reason
    assert "runner-hook-added" not in rep.escalation_reason


def test_a_clean_round_still_merges(tmp_path):
    """没动 hook 的正常任务照常合并 —— 包括仓库本来就装了合法 hook 的情况。"""
    root = _repo(tmp_path)
    _hook(root, "pre-commit", "exit 0")  # 人装的，派发前就在
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    (ws / "a.py").write_text("x\ny\n")
    ran: list = []
    rep = _dispatch(tmp_path, ws, ran)
    assert ran, "干净轮次 check 该照常跑"
    assert rep.outcome.value == "merged", f"有合法 hook 的仓库被误拦：{rep}"
