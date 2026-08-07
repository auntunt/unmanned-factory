"""沙箱隔离测试。真跑 sandbox-exec，不 mock。

mock 掉的沙箱测试毫无价值：这一层的全部内容就是"操作系统是否真的拒绝了
那次写入"。断言我们生成的字符串长什么样，只能证明我们会拼字符串。
所以下面每条断言都让真的 /usr/bin/sandbox-exec 去写真的文件。
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

import pytest

from factory.harness.sandbox import (
    SandboxPolicy,
    available,
    git_dir,
    policy_for,
    wrap,
)
from factory.harness.sandbox import prepare as sandbox_prepare

pytestmark = pytest.mark.skipif(
    not available() or sys.platform != "darwin",
    reason="Seatbelt 只在 macOS 上有",
)


def run_sandboxed(policy: SandboxPolicy, shell_cmd: str, *, cwd: Path):
    with contextlib.ExitStack() as stack:
        argv = wrap(["/bin/sh", "-c", shell_cmd], policy, stack=stack)
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=60
        )


# ── 核心：写入边界 ────────────────────────────────────────────────────────

def test_write_inside_workspace_succeeds(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = SandboxPolicy(workspace=ws)
    proc = run_sandboxed(policy, "echo hi > a.txt", cwd=ws)
    assert proc.returncode == 0, proc.stderr
    assert (ws / "a.txt").read_text().strip() == "hi"


def test_write_outside_workspace_is_denied(tmp_path):
    """本文件的主命题。这条挂了，整个模块没有意义。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    target = tmp_path / "outside.txt"
    policy = SandboxPolicy(workspace=ws, allow_tmp=False)
    proc = run_sandboxed(policy, f"echo bad > {target}", cwd=ws)
    assert proc.returncode != 0
    assert not target.exists(), "沙箱外的文件被写出来了"


def test_home_is_not_writable(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    marker = Path.home() / ".factory-sandbox-leak-test"
    policy = SandboxPolicy(workspace=ws, allow_tmp=False)
    proc = run_sandboxed(policy, f"echo bad > {marker}", cwd=ws)
    assert proc.returncode != 0
    assert not marker.exists(), f"$HOME 被写进去了：{marker}"


def test_extra_writable_is_honoured(tmp_path):
    ws = tmp_path / "ws"
    extra = tmp_path / "cache"
    ws.mkdir()
    extra.mkdir()
    policy = SandboxPolicy(workspace=ws, writable=(extra,))
    proc = run_sandboxed(policy, f"echo ok > {extra}/x.txt", cwd=ws)
    assert proc.returncode == 0, proc.stderr
    assert (extra / "x.txt").exists()


def test_denied_overrides_writable(tmp_path):
    """deny 必须能盖掉 allow —— policy_for 的工厂仓库保护全靠这个顺序。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    protected = ws / "keepout"
    protected.mkdir()
    policy = SandboxPolicy(workspace=ws, denied=(protected,))
    assert run_sandboxed(policy, "echo ok > fine.txt", cwd=ws).returncode == 0
    proc = run_sandboxed(policy, f"echo bad > {protected}/x.txt", cwd=ws)
    assert proc.returncode != 0
    assert not (protected / "x.txt").exists()


# ── 控制面保护 ───────────────────────────────────────────────────────────

def test_factory_own_code_is_not_writable(tmp_path):
    """worker 不能改编排层自己的分级规则。

    这是沙箱在本项目里最实际的作用：worker 有权改代码，而 oracle_rules.yaml
    也是代码。能改它就等于能删掉 D 类规则，下一次派发就没有硬闸门了。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    rules = Path(__file__).resolve().parents[1] / "factory/grading/oracle_rules.yaml"
    assert rules.exists(), "规则文件路径变了，这条测试要跟着改"
    before = rules.read_bytes()

    policy = policy_for(ws)
    proc = run_sandboxed(policy, f"echo '# evil' >> {rules}", cwd=ws)
    assert proc.returncode != 0
    assert rules.read_bytes() == before, "分级规则被 worker 改了"


def test_policy_for_denies_factory_root_even_if_also_writable(tmp_path):
    """有人把工厂仓库塞进 extra_writable 时，deny 仍然要赢。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = Path(__file__).resolve().parents[1]
    policy = policy_for(ws, extra_writable=(root,))
    target = root / ".factory-sandbox-should-not-exist"
    proc = run_sandboxed(policy, f"echo bad > {target}", cwd=ws)
    assert proc.returncode != 0
    assert not target.exists()


# ── git worktree 兼容 ────────────────────────────────────────────────────

def make_repo_with_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args, cwd=repo):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)

    g("init", "-q", ".")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (repo / "f.txt").write_text("base\n")
    g("add", "f.txt")
    g("commit", "-qm", "base")
    wt = tmp_path / "wt"
    g("worktree", "add", "-q", "-b", "sbx", str(wt), "HEAD")
    return repo, wt


def test_git_dir_resolves_to_common_dir_in_worktree(tmp_path):
    repo, wt = make_repo_with_worktree(tmp_path)
    assert git_dir(wt) == (repo / ".git").resolve()


def test_git_commit_works_inside_sandboxed_worktree(tmp_path):
    """worktree 的 .git 是指向父仓库的文件，git 要写父仓库的 refs/index。

    只放开 workspace 会让 git 直接 fatal（index.lock 建不出来）。
    这条测试锁住 policy_for 会把 --git-common-dir 加进白名单。
    """
    _repo, wt = make_repo_with_worktree(tmp_path)
    policy = policy_for(wt)
    proc = run_sandboxed(
        policy,
        "echo new > n.txt && git add -A && "
        "git -c user.email=t@e -c user.name=t commit -qm work",
        cwd=wt,
    )
    assert proc.returncode == 0, f"沙箱里 git 挂了：{proc.stderr}"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wt, capture_output=True, text=True
    )
    assert "work" in log.stdout


def test_capture_diff_works_inside_sandbox(tmp_path):
    """capture_diff 用 `git add -A -N`，它也要写父仓库 index。"""
    _repo, wt = make_repo_with_worktree(tmp_path)
    policy = policy_for(wt)
    proc = run_sandboxed(policy, "echo x > n.txt && git add -A -N", cwd=wt)
    assert proc.returncode == 0, proc.stderr


# ── 策略构造：错误要早报 ─────────────────────────────────────────────────

def test_relative_workspace_is_rejected():
    """相对路径在 sbpl 里静默匹配不到任何东西 —— 策略加载成功但一个字写不出。"""
    with pytest.raises(ValueError, match="绝对路径"):
        SandboxPolicy(workspace=Path("relative/ws"))


def test_relative_extra_path_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="绝对路径"):
        SandboxPolicy(workspace=tmp_path, writable=(Path("rel"),))


def test_paths_never_appear_inline_in_the_profile(tmp_path):
    """路径只能经 -D 传。拼进模板的话，一个含 `)` 的目录名就能把 deny 挤出策略。

    那种失效最坏：策略仍然加载成功，只是少了一条禁令，没人会看见。
    """
    weird = tmp_path / 'ws with "quote" and ) paren'
    weird.mkdir()
    src, params = SandboxPolicy(workspace=weird).profile()
    assert str(weird) not in src, "路径被拼进了 sbpl 源文本"
    assert any(str(weird.resolve()) in p for p in params)
    assert src.count("(deny file-write*)") == 1


def test_weird_directory_name_still_confines(tmp_path):
    """上一条测的是构造，这条测真 OS 行为 —— 怪名字目录下禁令依然生效。"""
    weird = tmp_path / 'ws with "quote" and ) paren'
    weird.mkdir()
    outside = tmp_path / "outside.txt"
    policy = SandboxPolicy(workspace=weird, allow_tmp=False)
    proc = run_sandboxed(policy, f"echo bad > {outside}", cwd=weird)
    assert proc.returncode != 0
    assert not outside.exists()


def test_wrap_prefixes_argv_and_keeps_command_intact(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with contextlib.ExitStack() as stack:
        argv = wrap(["claude", "-p", "do it"], SandboxPolicy(workspace=ws), stack=stack)
    assert argv[0].endswith("sandbox-exec")
    assert argv[-3:] == ["claude", "-p", "do it"], "被包的命令不能被改写"


def test_profile_file_is_cleaned_up_when_stack_closes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with contextlib.ExitStack() as stack:
        argv = wrap(["/bin/true"], SandboxPolicy(workspace=ws), stack=stack)
        profile = Path(argv[argv.index("-f") + 1])
        assert profile.exists()
    assert not profile.exists(), "策略文件泄漏了"


# ── 不是硬闸门的旁路 ─────────────────────────────────────────────────────

def test_sandbox_does_not_relax_the_hard_gate(tmp_path):
    """沙箱是运行时兜底，不能让 D 类任务变得可以派发。

    两层挡的是不同东西：硬闸门在派发**前**拦住不可逆操作（根本不启动
    worker）；沙箱只在 worker 跑起来之后限制它写本机文件。沙箱挡不住一个
    已经拿到生产凭据的进程去调远端 API —— 所以「有沙箱了所以 D 类可以放宽」
    这条推理是错的。这里用真的分级引擎钉住它。
    """
    from factory.grading.rules import GradingEngine
    from factory.audit.models import OracleClass

    grade = GradingEngine.default().grade((), ("prod_deploy",))
    assert grade.oracle_class == OracleClass.D
    assert grade.hard_gate is True
    assert grade.unmanned_allowed is False


# ── 经 adapter 的端到端 ──────────────────────────────────────────────────

def git_workspace(path: Path) -> Path:
    """带一个基线 commit 的仓库。capture_diff 要跟 HEAD 比，没 HEAD 就拿不到改动。"""
    path.mkdir(parents=True, exist_ok=True)

    def g(*args):
        subprocess.run(["git", *args], cwd=path, capture_output=True, check=True)

    g("init", "-q", ".")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (path / ".keep").write_text("", encoding="utf-8")
    g("add", ".keep")
    g("commit", "-qm", "base")
    return path


def make_escaping_worker(tmp_path, target: Path) -> Path:
    """一个"跑飞的" worker：先在 workspace 里正常干活，再试着写外面。"""
    script = tmp_path / "escaping_worker.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo legit > in_workspace.txt\n"
        f"echo escaped > {target} 2>/dev/null\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_shell_adapter_with_sandbox_confines_the_worker(tmp_path):
    """端到端：经 ShellAdapter 派发，worker 的越界写被 OS 拦掉。

    上面那些测试用的是 /bin/sh -c，这条走真正的派发路径 —— 证明包装点接在了
    对的位置，而不只是 sandbox.wrap() 自己能用。
    """
    from factory.harness.base import Limits
    from factory.harness.shell import ShellAdapter
    from factory.task import Task

    ws = git_workspace(tmp_path / "ws")
    escape_target = tmp_path / "escaped.txt"
    worker = make_escaping_worker(tmp_path, escape_target)

    adapter = ShellAdapter([str(worker)], name="escaper", sandbox=True)
    result = adapter.run(Task(task_id="T-esc", prompt="x"), ws, Limits(timeout_s=60))

    assert (ws / "in_workspace.txt").exists(), "沙箱不该妨碍正常工作"
    assert not escape_target.exists(), "worker 写到了 workspace 外面"
    assert "in_workspace.txt" in result.changed_paths


def test_shell_adapter_without_sandbox_does_not_confine(tmp_path):
    """反向验证：关掉沙箱，同一个 worker 就真的能写到外面去。

    没有这条，上面那条测试无法区分"沙箱起作用了"和"worker 本来就没写成功"。
    """
    from factory.harness.base import Limits
    from factory.harness.shell import ShellAdapter
    from factory.task import Task

    ws = git_workspace(tmp_path / "ws")
    escape_target = tmp_path / "escaped_unsandboxed.txt"
    worker = make_escaping_worker(tmp_path, escape_target)

    ShellAdapter([str(worker)], name="escaper", sandbox=False).run(
        Task(task_id="T-esc2", prompt="x"), ws, Limits(timeout_s=60)
    )
    assert escape_target.exists(), (
        "不开沙箱时 worker 应该能写到外面 —— 写不出来说明上一条测试是假绿"
    )


def test_audit_records_whether_sandbox_was_on(tmp_path):
    """审计必须能区分"隔离下跑出来的"和"没隔离跑出来的"。

    否则两次 `merged` 长得一模一样，事后没人分得清。走 harness_version 后缀
    而不是加一列：加列要改 schema，而改 schema 本身是 D 类不可逆操作。
    """
    from factory.harness.shell import ShellAdapter

    worker = tmp_path / "w.sh"
    worker.write_text("#!/bin/sh\necho hi > out.txt\n", encoding="utf-8")
    worker.chmod(0o755)

    on = ShellAdapter([str(worker)], name="w", sandbox=True).version()
    off = ShellAdapter([str(worker)], name="w", sandbox=False).version()
    assert on.endswith("+sandbox")
    assert not off.endswith("+sandbox")
    assert len(on) <= 64, "harness_version 列宽 64，不能写爆"


def test_version_tag_truncates_long_versions():
    from factory.harness.sandbox import tag_version

    long = "v" * 200
    assert len(tag_version(long, True)) <= 64
    assert tag_version(long, True).endswith("+sandbox")
    assert len(tag_version(long, False)) <= 64


def test_tmp_is_private_not_the_whole_tmp_root(tmp_path):
    """回归：allow_tmp 不能放开整个 TMPDIR 根。

    实测踩到过（2026-08-07）：策略里写 `subpath(tempfile.gettempdir())`，
    /var/folders 下住着所有别的任务的临时目录，于是并行派发时一个 worker
    能写进另一个 worker 的临时文件 —— 越界写居然成功了，是 adapter 端到端
    测试才发现的。prepare() 现在给每次派发单独开目录并覆盖 TMPDIR。
    """
    import tempfile as _tf

    ws = git_workspace(tmp_path / "ws")
    shared_tmp_victim = Path(_tf.gettempdir()) / "factory-sandbox-tmp-leak-probe"
    shared_tmp_victim.unlink(missing_ok=True)

    with contextlib.ExitStack() as stack:
        argv, env = sandbox_prepare(["/bin/sh", "-c", f"echo bad > {shared_tmp_victim}"], ws, stack)
        proc = subprocess.run(
            argv, cwd=ws, env={**__import__("os").environ, **env},
            capture_output=True, text=True, timeout=60,
        )
        private = Path(env["TMPDIR"])
        assert private.is_dir(), "prepare 应该给出私有 TMPDIR"
        # 私有目录本身必须可写，否则 worker 的工具链会以看不懂的方式挂掉
        assert subprocess.run(
            wrap(["/bin/sh", "-c", f"echo ok > {private}/probe.txt"],
                 SandboxPolicy(workspace=ws, tmp_dir=private), stack=stack),
            cwd=ws, capture_output=True,
        ).returncode == 0

    assert proc.returncode != 0
    assert not shared_tmp_victim.exists(), "worker 写进了共享 TMPDIR 根"


# ── transcript 边界：projects 可写、~/.claude 其余不可写 ──────────────────

def test_transcript_dir_is_writable(tmp_path):
    """worker 必须能写会话记录，否则审计悄悄丢东西。

    实测踩到过（2026-08-07）：整个 ~/.claude 被拒时，`claude` 不报错，只是
    transcript 写不出来 —— find_transcript() 返回 None，审计里的 tool_calls
    静默变空。**审计少了东西比大声失败更糟**：merged 看起来一样，但事后
    没法还原 worker 到底动了什么。是 e2e 冒烟测试才发现的。
    """
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        pytest.skip("本机没有 ~/.claude/projects")

    ws = git_workspace(tmp_path / "ws")
    probe = projects / ".factory-sandbox-transcript-probe"
    probe.unlink(missing_ok=True)
    try:
        proc = run_sandboxed(policy_for(ws), f"echo ok > {probe}", cwd=ws)
        assert proc.returncode == 0, f"transcript 目录不可写：{proc.stderr}"
        assert probe.exists()
    finally:
        probe.unlink(missing_ok=True)


def test_claude_settings_stays_denied(tmp_path):
    """放开 projects 不等于放开整个 ~/.claude。

    settings.json 就在 projects 隔壁，能写它就能塞 hook —— 那是编排层
    下一次启动时的任意代码执行，而且是以编排层的身份、在沙箱**外面**跑。
    换句话说，放宽这条边界等于给沙箱开一条延迟生效的越狱通道。
    所以 policy_for 只放 projects 子目录，这条测试钉住"隔壁仍然被拒"。
    """
    ws = git_workspace(tmp_path / "ws")
    claude_dir = Path.home() / ".claude"
    if not claude_dir.is_dir():
        pytest.skip("本机没有 ~/.claude")

    settings = claude_dir / "settings.json"
    before = settings.read_bytes() if settings.exists() else None

    proc = run_sandboxed(policy_for(ws), f"echo '// evil' >> {settings}", cwd=ws)
    assert proc.returncode != 0, "settings.json 可写 —— 等于允许注入 hook"
    after = settings.read_bytes() if settings.exists() else None
    assert after == before, "settings.json 被改了"


def test_arbitrary_dotfile_in_claude_dir_stays_denied(tmp_path):
    """上一条只测了 settings.json 这一个文件名。

    真正的边界是"~/.claude 下除 projects 以外都不可写" —— 否则日后
    Claude Code 新增一个配置文件，这层保护就凭文件名漏掉了。
    """
    ws = git_workspace(tmp_path / "ws")
    claude_dir = Path.home() / ".claude"
    if not claude_dir.is_dir():
        pytest.skip("本机没有 ~/.claude")

    target = claude_dir / ".factory-sandbox-should-not-exist"
    target.unlink(missing_ok=True)
    proc = run_sandboxed(policy_for(ws), f"echo bad > {target}", cwd=ws)
    assert proc.returncode != 0
    assert not target.exists(), f"~/.claude 下被写出了文件：{target}"
