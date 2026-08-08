"""`.git/config` 里一行设置让监工的输入整体归零。

洞的形状：`.git/config` 在 sandbox 里可写（git_dir 必须可写，capture_diff
要写 index.lock）。config 里有开关能直接改变 `git diff` 的行为，而这些设置
**不在任何 diff 里** —— 前面十二个洞的检测全都基于「文件树 / 索引 / 属性」，
没有一个看 config。

两条实测成立的路：
  diff.external=<脚本>     → `git diff HEAD` 返回空字符串（len=0），而
                             `--name-only` 不走 external → changed_paths 照常
                             非空 → 「空 diff」那道判定也不响
  core.attributesFile=<外部>→ 效果同第七个洞，但 `check-attr --source=HEAD`
                             也读这个设置，基线和现状都是「关掉」→ 第七道
                             闸门差异为空，被绕过

所以判据是「这一轮 config 变了」，不逐个堵开关名 —— git 的 config 键有几百个。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.workspace import (
    capture_diff,
    changed_config,
    diff_suppressed,
    git_config,
    index_skipped,
    runner_hooks,
    shadow_code,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _external(root: Path) -> None:
    """worker 挂上 diff.external —— 一个什么都不输出的脚本。"""
    sh = root / "fake-diff.sh"
    sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sh.chmod(0o755)
    _git(root, "config", "diff.external", "./fake-diff.sh")


# --- 一、先证明洞是真的 ---------------------------------------------------


def test_the_hole_diff_external_empties_the_diff(tmp_path: Path) -> None:
    """挂上 diff.external 之后，监工拿到的 diff 是空字符串。"""
    root = _repo(tmp_path)
    (root / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")
    _external(root)

    diff, paths = capture_diff(root)

    assert diff == "", f"洞不存在了？diff 还有 {len(diff)} 字节"
    assert "pdb.set_trace" not in diff
    # 路径照常非空 —— 这是「空 diff」那道判定不响的原因。
    assert "a.py" in paths


def test_the_empty_diff_gate_does_not_fire(tmp_path: Path) -> None:
    """`if not result.changed_paths` 判的是路径，不是 diff。

    这一条钉的是「为什么已有的空 diff 判定救不了」：它看 changed_paths，
    而 --name-only 不走 external，所以那道判定完全正常地放行。
    """
    root = _repo(tmp_path)
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    _external(root)

    diff, paths = capture_diff(root)

    assert not diff
    assert paths, "路径也空了的话，已有的空 diff 判定就够用，不需要这个洞"


def test_the_hole_all_existing_gates_are_blind(tmp_path: Path) -> None:
    """前面十二个洞的检测全都看不见 config 的改动。

    这不是「多一道保险」，是这个洞唯一的证据：没有任何一道现有闸门会响。
    """
    root = _repo(tmp_path)
    (root / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")
    _external(root)
    _, paths = capture_diff(root)

    assert shadow_code(root) == ()
    assert runner_hooks(root) == ()
    assert index_skipped(root) == frozenset()
    assert diff_suppressed(root, paths) == ()


def test_the_hole_attributes_file_bypasses_the_seventh_gate(tmp_path: Path) -> None:
    """`core.attributesFile` 指向仓库外，第七道闸门差异为空。

    第七道闸门的基线是 `check-attr --source=HEAD` —— 而那个命令**也**读
    core.attributesFile，于是「HEAD 里的属性」和「现在的属性」都是「关掉」，
    对比出来没有差异。这是为什么判 config 不能靠扩第七道闸门。
    """
    root = _repo(tmp_path)
    outside = tmp_path / "outside-attrs"
    outside.write_text("*.py -diff\n", encoding="utf-8")
    _git(root, "config", "core.attributesFile", str(outside))
    (root / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")

    diff, paths = capture_diff(root)

    assert "pdb.set_trace" not in diff, "洞不存在了？"
    assert "Binary files" in diff
    assert diff_suppressed(root, paths) == (), "第七道闸门居然抓到了？那可以不加这道"


# --- 二、判据本身 ---------------------------------------------------------


def test_a_clean_round_reports_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = git_config(root)
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    capture_diff(root)
    assert changed_config(before, git_config(root)) == ()


def test_capture_diff_does_not_touch_config(tmp_path: Path) -> None:
    """基线可用的前提：我们自己的流程不改 config。

    `capture_diff` 会写 index（`add -A -N`），要是它顺带动了 config，
    这道闸门就在每个任务上都响。
    """
    root = _repo(tmp_path)
    before = git_config(root)
    (root / "new.py").write_text("y = 1\n", encoding="utf-8")
    capture_diff(root)
    assert git_config(root) == before


def test_a_new_key_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = git_config(root)
    _external(root)
    assert changed_config(before, git_config(root)) == ("diff.external",)


def test_a_changed_value_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = git_config(root)
    _git(root, "config", "user.name", "someone-else")
    assert changed_config(before, git_config(root)) == ("user.name",)


def test_a_deleted_key_is_reported(tmp_path: Path) -> None:
    """删也算：仓库里人设的 diff.algorithm 被删掉同样改变 diff 的形状。"""
    root = _repo(tmp_path)
    _git(root, "config", "diff.algorithm", "histogram")
    before = git_config(root)
    _git(root, "config", "--unset", "diff.algorithm")
    assert changed_config(before, git_config(root)) == ("diff.algorithm",)


def test_reverting_is_not_reported(tmp_path: Path) -> None:
    """改回去就不算 —— 这道闸门是可以自己修的，不是死刑。"""
    root = _repo(tmp_path)
    before = git_config(root)
    _external(root)
    _git(root, "config", "--unset", "diff.external")
    assert changed_config(before, git_config(root)) == ()


def test_a_value_containing_a_newline_is_one_record(tmp_path: Path) -> None:
    """值里有换行时仍然是一条 —— 这是 `--list -z` 而不是按行切的理由。

    按行切的话，`a\\nb` 会被拆成两条记录，第二条的 key 是 `b`，于是每次
    比对都有一条幽灵键在动。
    """
    root = _repo(tmp_path)
    before = git_config(root)
    _git(root, "config", "core.multi", "a\nb")

    now = git_config(root)
    assert changed_config(before, now) == ("core.multi",)
    assert dict(now)["core.multi"] == "a\nb"


def test_pre_existing_keys_are_not_reported(tmp_path: Path) -> None:
    """仓库本来就有的设置不算这一轮的改动。

    外部仓库可能真的设了 diff.external（有人拿它接自定义 diff 工具）。
    判「有没有」会让那种仓库上每个任务都判红。
    """
    root = _repo(tmp_path)
    _external(root)
    before = git_config(root)  # 基线里已经带着它
    (root / "a.py").write_text("x = 9\n", encoding="utf-8")
    capture_diff(root)
    assert changed_config(before, git_config(root)) == ()


def test_global_config_is_not_read(tmp_path: Path) -> None:
    """只读 --local。global 层不在 workspace 里，worker 改不到。

    读进来的后果是把「这台机器的设置」当成任务的改动：本机 global 里有
    user.name / core.excludesfile 之类，每个仓库都会带一串恒定噪音。
    """
    root = _repo(tmp_path)
    keys = {k for k, _ in git_config(root)}
    local = {
        line.partition("=")[0]
        for line in _git(root, "config", "--local", "--list").splitlines()
        if line
    }
    assert keys == local


def test_a_non_repo_is_empty_not_an_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_config(plain) == frozenset()


def test_this_repo_has_a_stable_config(tmp_path: Path) -> None:
    """本仓库上连取两次一致 —— 一道在自己仓库上会抖的闸门等于一道关掉的闸门。"""
    here = Path(__file__).resolve().parent.parent
    assert changed_config(git_config(here), git_config(here)) == ()


# --- 三、接线：真跑一轮 dispatcher ----------------------------------------


def _dispatch(tmp_path: Path, ws: Path, checks_ran: list, *, cfg: tuple | None = None):
    """跑一轮真 dispatcher。`cfg` 模拟 worker 在派发**期间**改 .git/config。"""
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class ConfigWriting(FakeAdapter):
        def run(self, task, workspace, limits, *, model=None):
            if cfg is not None:
                subprocess.run(
                    ("git", "config", *cfg), cwd=workspace, check=True,
                    capture_output=True,
                )
            return super().run(task, workspace, limits, model=model)

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(
        task_id="T-cfg",
        prompt="改点东西",
        checks=(CheckSpec(name="ok", command="true"),),
    )
    d = Dispatcher(
        adapter=ConfigWriting([_result(paths=("a.py",))]),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Recording(),
    )
    return d.run(task, ws)


def _worktree(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    (ws / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")
    return ws


def test_setting_diff_external_blocks_the_merge(tmp_path: Path) -> None:
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], cfg=("diff.external", "true"))
    assert rep.outcome.value != "merged", f"改了 config 却合并了：{rep}"
    assert "git-config-touched" in rep.escalation_reason


def test_the_claim_names_the_key(tmp_path: Path) -> None:
    """打回的话里要有键名，worker 才知道该撤哪一条。**不给值** —— 值里可能有路径。

    断言的是**小写**的键名：`git config --local --list` 会把 section 和 name
    归一化成小写（实测 `core.attributesFile` 出来是 `core.attributesfile`）。
    这不影响判定 —— 基线和现状走同一条 --list，比较是一致的；也不影响 worker
    动手 —— git 的 section/name 查找本身大小写不敏感，`--unset
    core.attributesfile` 照样撤得掉。所以不去还原原始大小写。
    """
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], cfg=("core.attributesFile", "/tmp/secret-path"))
    assert "core.attributesfile" in rep.escalation_reason
    assert "/tmp/secret-path" not in rep.escalation_reason, "键值被递进 prompt 了"


def test_checks_do_not_run_when_config_changed(tmp_path: Path) -> None:
    """判据的载体被动过之后，那份绿不值得花钱去取。"""
    ran: list = []
    ws = _worktree(tmp_path)
    _dispatch(tmp_path, ws, ran, cfg=("diff.external", "true"))
    assert ran == [], f"config 被改了还去跑 check：{ran}"


def test_a_clean_round_still_merges(tmp_path: Path) -> None:
    """误报面：不动 config 的一轮照常合并。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], cfg=None)
    assert rep.outcome.value == "merged", f"干净的一轮被拦了：{rep}"


def test_the_other_gates_stay_quiet(tmp_path: Path) -> None:
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], cfg=("diff.external", "true"))
    for other in ("shadow-code", "runner-hook-added", "git-hook-touched",
                  "diff-suppressed", "index-skip-flag"):
        assert other not in rep.escalation_reason, f"{other} 也响了"


# --- 四、第四层 config：worktree 层 -----------------------------------------
#
# 第十三道闸门自己的盲区：它只读 `--local`，而 git 有第四层 `config.worktree`
# （`.git/worktrees/<名>/config.worktree`）。工厂正是用 worktree 跑并行任务的。


def _wt_repo(tmp_path: Path, *, extensions: bool) -> Path:
    """建一个 worktree，`extensions` 决定 worktreeConfig 开没开。"""
    root = _repo(tmp_path)
    if extensions:
        _git(root, "config", "extensions.worktreeConfig", "true")
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    return tmp_path / "wt"


def test_the_hole_worktree_layer_was_invisible_to_local(tmp_path: Path) -> None:
    """洞本身：`--local` 读不到 worktree 层，所以只读 local 的闸门看不见它。

    刻意断言的是 **git 的行为**（`--local --list` 里没有这个键），不是我的
    函数 —— 这条测试要证的是「视野差存在」，删掉我的修法它照样该通过。
    """
    ws = _wt_repo(tmp_path, extensions=True)
    _git(ws, "config", "--worktree", "diff.external", "/bin/true")

    local_only = _git(ws, "config", "--local", "--list")
    assert "diff.external" not in local_only, "git 行为变了？那这个洞不存在了"
    assert "diff.external" in _git(ws, "config", "--list")


def test_the_hole_empties_the_diff_through_the_worktree_layer(tmp_path: Path) -> None:
    """走 worktree 层设 diff.external，效果和走 local 一样：diff 全空。"""
    ws = _wt_repo(tmp_path, extensions=True)
    fake = tmp_path / "fake.sh"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    _git(ws, "config", "--worktree", "diff.external", str(fake))
    (ws / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")

    diff, paths = capture_diff(ws)
    assert diff == "", f"diff 没被清空：{diff[:200]!r}"
    assert paths == ("a.py",), "路径还在 —— 所以「空 diff」那道判定不响"


def test_the_worktree_layer_is_caught(tmp_path: Path) -> None:
    ws = _wt_repo(tmp_path, extensions=True)
    before = git_config(ws)
    _git(ws, "config", "--worktree", "diff.external", "/bin/true")
    assert changed_config(before, git_config(ws)) == ("diff.external",)


def test_undoing_the_worktree_layer_is_quiet(tmp_path: Path) -> None:
    """撤回去就不该再响 —— 判据是「这一轮变了」，不是「有没有设过」。"""
    ws = _wt_repo(tmp_path, extensions=True)
    before = git_config(ws)
    _git(ws, "config", "--worktree", "diff.external", "/bin/true")
    _git(ws, "config", "--worktree", "--unset", "diff.external")
    assert changed_config(before, git_config(ws)) == ()


def test_enabling_the_extension_this_round_is_itself_caught(tmp_path: Path) -> None:
    """前提条件本身也被拦：开 extensions 要写 `.git/config`。

    所以这个洞的危害面只是「仓库**本来**就开着 worktreeConfig」（sparse-checkout
    的常见用法），不是任何仓库都能走。
    """
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    before = git_config(ws)
    _git(ws, "config", "extensions.worktreeConfig", "true")
    assert "extensions.worktreeconfig" in changed_config(before, git_config(ws))


def test_a_worktree_without_the_extension_is_not_an_error(tmp_path: Path) -> None:
    """未开 extensions 的 worktree 上 `--worktree` 会 rc=128，必须当空层。

    实测的报错：`无法读取配置文件 '.git/config.worktree'`。让它冒出去的话，
    每个普通 worktree 任务都会炸在取基线这一步。
    """
    ws = _wt_repo(tmp_path, extensions=False)
    assert git_config(ws), "普通 worktree 上读成空了 —— local 层也丢了"
    assert changed_config(git_config(ws), git_config(ws)) == ()
