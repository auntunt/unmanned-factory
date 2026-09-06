"""伪造绿不需要新增文件 —— 所以判「新增了什么」的检测有天花板。

`runner_hooks` 判的是「这一轮**新增**了 runner 自动加载的文件」。三条实测
都绕过它：

  改**已存在**的 tests/conftest.py，追加 makereport hookwrapper   → 退出码 0
  同一个 conftest 里 pytest_sessionfinish 改 session.exitstatus  → 退出码 0
  pyproject.toml 的 addopts = "-p myplug" 加载本地插件            → 退出码 0

前两条不是新增，第三条的文件名在 `_RUNNER_HOOKS` 的**刻意排除**名单里
（`pyproject.toml` 是任务合法要改的东西）。

所以这一条换个问法：不问 worker 干了什么，问**那份绿是真的吗** ——
往测试目录塞一条 `assert 1 == 2`，再跑同一条 check。红不起来就是假绿。
好处是不用枚举机制，将来的新写法只要效果是「红不起来」就会被抓到。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.verdict_probe import (
    CANARY_BODY,
    CANARY_NAME,
    applies_to,
    canary_target,
    judge_files_touched,
    probe,
)
from tests.conftest import PYTEST_CMD, git_setup

#: 用 PYTEST_CMD（= sys.executable -m pytest）而不是裸 "python -m pytest"：
#: 这些用例真的会把这串喂给 shell 跑，裸 python 会走 PATH 撞到别的 venv，
#: 于是「基线该是红的」之类的前置断言因为 pytest 没起来而集体假红。
CMD = f"{PYTEST_CMD} -q"

# 把 failed 改判成 passed。和 test_runner_hooks 里的 _FAKE_GREEN 同一手 ——
# 那边验的是「新增这个文件会被拦」，这边验的是「**改**已存在的文件也会被抓」。
_MAKEREPORT = """
import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    if rep.outcome == "failed":
        rep.outcome = "passed"
        rep.longrepr = None
"""

# 另一条路：不碰单条测试的裁决，直接改整场的退出码。
_SESSIONFINISH = """
def pytest_sessionfinish(session, exitstatus):
    session.exitstatus = 0
"""


def _repo(tmp_path: Path, *, passing: bool = True) -> Path:
    """一个有 tests/ 的仓库。`passing=True` 时基线是绿的。"""
    root = tmp_path / "wp"
    (root / "tests").mkdir(parents=True)
    body = "def test_x(): pass\n" if passing else "def test_x(): assert 0\n"
    (root / "tests" / "test_a.py").write_text(body, encoding="utf-8")
    # conftest.py 一开始就存在 —— 这是这条洞的前提（改而不是新增）。
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n", encoding="utf-8")
    git_setup(root)
    return root


def _sh(root: Path, cmd: str) -> int:
    """跑一条 shell 命令，只要退出码。

    这里故意不检查返回码 —— 被测对象就是「命令失败/假绿」本身，
    非零退出是数据不是异常。但要有 timeout，否则挂住的探针会拖死会话。
    """
    try:
        return subprocess.run(
            cmd, cwd=root, shell=True, capture_output=True, timeout=300
        ).returncode
    except subprocess.TimeoutExpired:  # pragma: no cover - 只在机器过载时走到
        raise AssertionError(f"探针命令超过 300s 没返回，在 {root}: {cmd}") from None


# ---------- 先证明洞存在 ----------

def test_the_hole_is_real_editing_an_existing_conftest_fakes_green(tmp_path):
    """改**已存在**的 conftest.py → 必然失败的测试拿到退出码 0。

    没有这条，下面的测试只是在测一个函数的返回值。
    """
    root = _repo(tmp_path, passing=False)
    assert _sh(root, CMD) != 0, "基线该是红的"
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + _MAKEREPORT, encoding="utf-8")
    assert _sh(root, CMD) == 0, "改 conftest 之后该变绿（洞的前提）"


def test_runner_hooks_does_not_see_an_edited_conftest(tmp_path):
    """而 runner_hooks 报不出来 —— 它只判新增。

    这条是「为什么要多一道检测」的证据，不是重复测试。
    """
    from factory.harness.workspace import runner_hooks

    root = _repo(tmp_path, passing=False)
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + _MAKEREPORT, encoding="utf-8")
    assert runner_hooks(root) == (), "改动被 runner_hooks 抓到了？那这道检测就不必要"


def test_pyproject_addopts_can_load_an_arbitrary_plugin(tmp_path):
    """第三条路：addopts = "-p myplug" 把本地插件塞进 pytest。

    `--ignore=tests` 之类拿到的是退出码 5（no tests collected），不是 0，
    所以真正能伪造绿的是加载插件这一手 —— 实测枚举过 8 种 addopts。
    """
    root = _repo(tmp_path, passing=False)
    (root / "myplug.py").write_text(_MAKEREPORT, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-p myplug"\n', encoding="utf-8")
    assert _sh(root, CMD) == 0
    from factory.harness.workspace import _RUNNER_HOOKS
    assert "pyproject.toml" not in _RUNNER_HOOKS, (
        "pyproject.toml 是任务合法要改的东西，它不该在 _RUNNER_HOOKS 里")


# ---------- 探针 ----------

def test_a_clean_tree_probes_as_real_green(tmp_path):
    """干净树：金丝雀红了 → 这份绿是真的。"""
    res = probe(_repo(tmp_path), CMD)
    assert not res.fake_green
    assert not res.skipped
    assert res.exit_code != 0


def test_an_edited_conftest_is_caught(tmp_path):
    root = _repo(tmp_path)
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + _MAKEREPORT, encoding="utf-8")
    res = probe(root, CMD)
    assert res.fake_green, res
    assert res.exit_code == 0


def test_faking_the_session_exit_status_is_caught(tmp_path):
    """另一种机制，同一个判据 —— 探针不枚举机制，只看效果。"""
    root = _repo(tmp_path)
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + _SESSIONFINISH, encoding="utf-8")
    assert probe(root, CMD).fake_green


def test_an_addopts_plugin_is_caught(tmp_path):
    root = _repo(tmp_path)
    (root / "myplug.py").write_text(_MAKEREPORT, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-p myplug"\n', encoding="utf-8")
    assert probe(root, CMD).fake_green


def test_a_legitimate_conftest_edit_is_not_flagged(tmp_path):
    """加一个夹具不是伪造 —— 误拒率靠这条守。"""
    root = _repo(tmp_path)
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + "\n@pytest.fixture\ndef h():\n    return 1\n",
                  encoding="utf-8")
    assert not probe(root, CMD).fake_green


def test_a_legitimate_pyproject_edit_is_not_flagged(tmp_path):
    """加一个 marker 也不是伪造。"""
    root = _repo(tmp_path)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["slow: x"]\n', encoding="utf-8")
    assert not probe(root, CMD).fake_green


def test_the_canary_is_always_removed(tmp_path):
    """金丝雀留下来的后果不是「多个垃圾文件」：它会进下一轮的 diff，
    于是 changed_paths 里冒出一个没人要求过的文件，范围监工判越界，
    worker 拿到一条他看不懂的打回。"""
    root = _repo(tmp_path)
    probe(root, CMD)
    assert list(root.rglob(f"*{CANARY_NAME}*")) == []
    assert list(root.rglob("*factory_canary*")) == []
    out = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "canary" not in out, f"金丝雀进了 git status：{out}"


# ---------- 跳过路径：「没验」不等于「验过是真的」 ----------

def test_a_non_pytest_command_is_skipped_not_passed(tmp_path):
    """`test -f a.py` 不该因为 tests/ 里多一个文件而变红。

    skipped 和 fake_green=False 必须分开：混成一个 bool 会让「没验」在
    报表上和「验过了是真的」长得一样。
    """
    res = probe(_repo(tmp_path), "test -f README.md")
    assert res.skipped
    assert not res.fake_green
    assert "不是 pytest" in res.reason


def test_no_test_dir_is_skipped(tmp_path):
    """没有 tests/ 就不验，也**不造**一个。

    造出来的目录会进 git status，而这道探针跑在 capture_diff 之后、判绿
    之前 —— 多一个目录会让「提交的正好是监工审过的那一组」出现一个
    没人审过的成员。
    """
    root = tmp_path / "bare"
    root.mkdir()
    res = probe(root, CMD)
    assert res.skipped
    assert canary_target(root) is None
    assert not (root / "tests").exists(), "探针不该造测试目录"


def test_an_existing_canary_name_is_not_overwritten(tmp_path):
    """撞名字宁可不验也不覆盖别人的文件。"""
    root = _repo(tmp_path)
    target = canary_target(root)
    target.write_text("# 别人的文件\n", encoding="utf-8")
    res = probe(root, CMD)
    assert res.skipped
    assert target.read_text() == "# 别人的文件\n"


def test_the_canary_name_must_be_collected_by_pytest(tmp_path):
    """金丝雀文件名必须匹配 `test_*.py`，否则 pytest 压根不看它。

    第一版把它叫 `_factory_canary_.py` 放在仓库根，pytest 不收集 →
    干净树上探针也返回 0 → 「红不起来」和「树是干净的」长得一模一样。
    实测踩过，所以这条断言盯的是文件名本身。
    """
    assert CANARY_NAME.startswith("test_")
    assert CANARY_NAME.endswith(".py")


def test_the_canary_body_fails_at_runtime_not_at_collection(tmp_path):
    """用 `assert 1 == 2` 而不是语法错误：语法错让整场收集失败（退出码 2），
    那和「check 本来就在 collect 阶段挂了」混在一起分不开。"""
    from factory.harness.verdict_probe import CANARY_BODY
    import ast
    ast.parse(CANARY_BODY)  # 必须是合法 Python
    root = _repo(tmp_path)
    (root / "tests" / CANARY_NAME).write_text(CANARY_BODY, encoding="utf-8")
    assert _sh(root, CMD) == 1, "该是 1（有测试失败），不是 2（收集失败）"


def test_judge_files_are_matched_by_basename(tmp_path):
    """conftest.py 放深一层照样生效，所以判文件名不判完整路径。"""
    assert judge_files_touched(
        ("src/a.py", "tests/sub/conftest.py", "pyproject.toml", "README.md")
    ) == ("tests/sub/conftest.py", "pyproject.toml")


def test_applies_to_matches_pytest_as_a_word(tmp_path):
    """认得宽但必须是独立的词 —— 别把 `pytest-cov` 这种字串也算进来。"""
    assert applies_to("python -m pytest -q")
    assert applies_to("uv run pytest")
    assert applies_to("cd sub && pytest")
    assert not applies_to("test -f a.py")
    assert not applies_to("npm test")
    assert not applies_to("echo pytest-cov")
    assert not applies_to("ruff check .")


# ---------- 接线 ----------

def _dispatch(tmp_path, root, *, changed=("tests/conftest.py",),
              check_cmd=CMD, probes=None):
    """跑一轮真 dispatcher。`probes` 收每次探针实际拿到的命令。"""
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    task = Task(task_id="T-fg", prompt="改点东西",
                checks=(CheckSpec(name="unit", command=check_cmd),))
    d = Dispatcher(adapter=FakeAdapter([_result(paths=changed)]),
                   store=AuditStore(tmp_path / "a.db"))
    if probes is not None:
        import factory.dispatcher as dm
        real = dm.probe

        def spy(root_, command, **kw):
            probes.append(command)
            return real(root_, command, **kw)

        dm.probe = spy
        try:
            return d.run(task, root)
        finally:
            dm.probe = real
    return d.run(task, root)


def test_a_faked_green_blocks_the_merge(tmp_path):
    """真跑一轮：check 自己是绿的（伪造的），但探针把它拦下来。"""
    root = _repo(tmp_path, passing=False)
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + _MAKEREPORT, encoding="utf-8")
    assert _sh(root, CMD) == 0, "前提：check 在伪造下是绿的"

    rep = _dispatch(tmp_path, root)
    assert rep.outcome.value != "merged", f"假绿合并了：{rep}"
    assert "fake-green" in rep.escalation_reason


def test_the_claim_names_the_file_that_was_touched(tmp_path):
    """打回的 claim 里要指名改了哪个文件，否则 worker 不知道修哪。"""
    root = _repo(tmp_path, passing=False)
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + _MAKEREPORT, encoding="utf-8")
    rep = _dispatch(tmp_path, root)
    assert "conftest.py" in rep.escalation_reason


def test_a_real_green_still_merges(tmp_path):
    """改了 conftest 但没伪造 → 探针放过 → 照常合并。误拒率靠这条守。"""
    root = _repo(tmp_path)
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + "\n@pytest.fixture\ndef h():\n    return 1\n",
                  encoding="utf-8")
    rep = _dispatch(tmp_path, root)
    assert rep.outcome.value == "merged", f"合法改 conftest 被误拦：{rep}"


def test_the_probe_does_not_run_when_no_judge_file_changed(tmp_path):
    """没动裁判权文件就不多花这一次探针。

    本仓库 55 次提交里 conftest.py / pyproject.toml 各改过 2 次（约 7%），
    所以绝大多数轮次这道检测是零成本的。
    """
    root = _repo(tmp_path)
    (root / "tests" / "test_b.py").write_text("def test_y(): pass\n",
                                              encoding="utf-8")
    seen: list[str] = []
    rep = _dispatch(tmp_path, root, changed=("tests/test_b.py",), probes=seen)
    assert seen == [], f"没动裁判权文件却跑了探针：{seen}"
    assert rep.outcome.value == "merged"


def test_the_probe_does_not_run_when_the_check_is_already_red(tmp_path):
    """回归监工判红时不验 —— 红的绿不用验，省一次探针。"""
    root = _repo(tmp_path, passing=False)  # check 真的红着
    seen: list[str] = []
    rep = _dispatch(tmp_path, root, probes=seen)
    assert seen == [], f"check 已经红了还跑探针：{seen}"
    assert rep.outcome.value != "merged"


def test_the_probe_is_wired_at_all(tmp_path):
    """接线本身：改了裁判权文件 + check 是绿的 → 探针一定被调用过。

    一道谁都没接上的闸门在报表上和接上了长得一样，所以这条盯的是
    「调用发生了」，和上面那些盯「结论对不对」的分开。
    """
    root = _repo(tmp_path)
    cf = root / "tests" / "conftest.py"
    cf.write_text(cf.read_text() + "\n# 只是注释\n", encoding="utf-8")
    seen: list[str] = []
    _dispatch(tmp_path, root, probes=seen)
    assert seen == [CMD], f"探针没接上：{seen}"


# ---------- 第十九个洞：金丝雀的落点由 worker 影响的文件树决定 ----------
#
# 前面所有伪造绿都要动一个**文件**，所以 diff 里至少有个影子。这条不动
# 文件：真测试在 `test/`（单数），worker 只 `mkdir tests/`（复数）。
# git 不跟踪空目录 —— changed_paths、added_paths、diff 里一个字都没有。
#
# `canary_target` 按 `_TEST_DIRS` 顺序取**第一个存在的**目录，于是金丝雀
# 落进那个空诱饵，`test/conftest.py` 的 hook 管不到它（conftest 只对自己
# 那棵子树生效），金丝雀正常红 → 探针报「验过了，这份绿是真的」。
#
# 比撞名那条严格更强：撞名是 skipped=True（没验），这条是 skipped=False
# + fake_green=False，**主动伪造出一次成功的验证**。


def _decoy_repo(tmp_path: Path) -> Path:
    """真测试在 test/，伪造绿的 conftest 也在 test/，另有一个空的 tests/。"""
    root = tmp_path / "decoy"
    (root / "test").mkdir(parents=True)
    (root / "test" / "test_a.py").write_text(
        "def test_x(): assert 0\n", encoding="utf-8")
    (root / "test" / "conftest.py").write_text(_MAKEREPORT, encoding="utf-8")
    git_setup(root)
    # 诱饵在提交之后造：git 不跟踪空目录，所以它不进任何一版 diff。
    (root / "tests").mkdir()
    return root


def test_the_hole_the_decoy_dir_leaves_no_trace_in_git(tmp_path):
    """诱饵目录对 git 完全隐形，且那份绿是假的。

    这条断言的是仓库的事实（不是探针的行为），所以修完之后它照旧成立 ——
    洞被堵不等于这条路不存在了，只是探针不再被它骗。
    """
    root = _decoy_repo(tmp_path)
    assert (root / "tests").is_dir()
    out = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "tests" not in out, f"git 竟然看见了空诱饵目录：{out!r}"
    assert _sh(root, CMD) == 0, "前提不成立：这份绿本来就该是假的"


def test_canary_target_picks_the_dir_that_holds_the_tests(tmp_path):
    """落点判据是「这儿有测试」，不是「这个目录存在」。

    `_TEST_DIRS` 的注释一直写着「必须放进**已经有测试**的目录」，但
    实现只判了 `is_dir()` —— 判据从来没被实现过。
    """
    root = _decoy_repo(tmp_path)
    target = canary_target(root)
    assert target is not None
    assert target.parent.name == "test", (
        f"金丝雀落进了空诱饵目录：{target}")


def test_a_decoy_test_dir_does_not_divert_the_canary(tmp_path):
    """效果判据：诱饵目录存在时，伪造绿仍然要被抓出来。

    修好之后金丝雀落进 test/，于是它自己也被那个 makereport hook 改判成
    passed —— hook 藏不住它把金丝雀也一起翻绿了这件事。
    """
    res = probe(_decoy_repo(tmp_path), CMD)
    assert res.fake_green, f"诱饵目录把探针骗过去了：{res}"
    assert not res.skipped


def test_an_empty_test_dir_is_skipped_not_verified(tmp_path):
    """只有空 tests/ 的仓库 = 没验，不能报成「验过是真的」。

    这是修落点带出来的**新增第五个 skip 出口**。它必须留在 skipped 那侧：
    报成 fake_green=False 就等于宣称验过了。
    """
    root = tmp_path / "hollow"
    (root / "tests").mkdir(parents=True)
    res = probe(root, CMD)
    assert res.skipped
    assert not res.fake_green
    assert canary_target(root) is None
    assert not list((root / "tests").iterdir()), "探针不该往空目录里留东西"


def test_a_leftover_canary_does_not_make_a_dir_a_valid_target(tmp_path):
    """上一轮残留的金丝雀不算「这个目录有测试」。

    `probe` 的 finally 会删它，但 SIGKILL 不走 finally，所以残留是真会发生的。
    残留被算进数就让一个空诱饵目录重新变成合法落点 —— 真目录只有 1 个测试
    时平票，顺序又偏向 `tests`，于是落点回到诱饵那边。

    后果是 fail-closed（落点已存在 → 撞名出口 → skipped），不是放行伪造绿。
    所以这一条挡的是**一次没必要的跳过**，不是一个洞。写下来免得下次
    有人以为它防的是更严重的东西。
    """
    root = tmp_path / "left"
    (root / "test").mkdir(parents=True)
    (root / "test" / "test_b.py").write_text("def test_b(): pass\n",
                                             encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / CANARY_NAME).write_text(CANARY_BODY, encoding="utf-8")

    target = canary_target(root)
    assert target is not None
    assert target.parent.name == "test", (
        f"残留金丝雀把诱饵目录算成了合法落点：{target}")


def test_a_tie_goes_to_the_first_test_dir(tmp_path):
    """两个候选目录测试数相同时，按 `_TEST_DIRS` 的顺序取，结果要稳定。

    平票取「后一个」在功能上未必更坏，但落点会随目录内容变来变去，
    而这道探针的排查全靠 reason 里那一行 —— 落点不稳定的时候，
    「上次验的是哪儿」这个问题没有答案。
    """
    root = tmp_path / "tie"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_a.py").write_text("def test_a(): pass\n",
                                              encoding="utf-8")
    (root / "test").mkdir()
    (root / "test" / "test_b.py").write_text("def test_b(): pass\n",
                                             encoding="utf-8")

    target = canary_target(root)
    assert target is not None
    assert target.parent.name == "tests", (
        f"平票没按 _TEST_DIRS 顺序取：{target}")


# ---------- F3 worker 自写 checks 全量过金丝雀 ----------
#
# 语法层的 vacuous_checks 看命令长什么样，拦得住 `echo ok`、`true`。
# 拦不住的是「命令完全正常、被测的东西是假的」：worker 新写一个
# `def test_ok(): assert True`，check 命令就是老老实实的 `pytest tests/`。
# 判决文件一个没动，所以按原来的抽样条件探针根本不跑，恒真自测一路绿到 merge。

def _dispatch_worker_checks(tmp_path, root, *, changed, check_cmd=CMD,
                            probes=None):
    """跑一轮真 dispatcher，check 走 **worker 自写**（.checks.json）那条路。

    和 `_dispatch` 的唯一区别是 task.checks 留空 —— 那是 dispatcher 判定
    「这组 check 是谁定的」的依据（`worker_authored = not task.checks`）。
    """
    import json

    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.task import Task
    from tests.test_dispatcher import FakeAdapter, _result

    # worker 在 workspace 里留下的判据文件，契约见 factory/checks_contract.py
    (root / ".checks.json").write_text(
        json.dumps({"checks": [{"name": "unit", "command": check_cmd}]}),
        encoding="utf-8")

    task = Task(task_id="T-fg-worker", prompt="改点东西")  # 注意：不传 checks
    d = Dispatcher(adapter=FakeAdapter([_result(paths=changed)]),
                   store=AuditStore(tmp_path / "aw.db"))
    if probes is not None:
        import factory.dispatcher as dm
        real = dm.probe

        def spy(root_, command, **kw):
            probes.append(command)
            return real(root_, command, **kw)

        dm.probe = spy
        try:
            return d.run(task, root)
        finally:
            dm.probe = real
    return d.run(task, root)


def test_worker_written_checks_are_always_probed(tmp_path):
    """worker 自写的 check 必跑探针，哪怕这轮没碰任何裁判权文件。

    这条是 F3 的核心：抽样条件（改了 conftest/pyproject 才验）对人写的
    check 成立，对 worker 自写的不成立 —— 它不需要动裁决装置就能造出假绿。
    """
    root = _repo(tmp_path)
    (root / "tests" / "test_b.py").write_text("def test_y(): pass\n",
                                              encoding="utf-8")
    seen: list[str] = []
    _dispatch_worker_checks(tmp_path, root, changed=("tests/test_b.py",),
                            probes=seen)
    assert seen, "worker 自写的 check 没过探针 —— 恒真自测能一路绿到 merge"


def test_a_worker_written_always_true_test_is_sent_back(tmp_path):
    """判据用例：worker 写一条恒真自测 → 被打回，不许 merge。

    构造的是真实的偷懒路径：改动本身什么都没修好（被测函数照旧是坏的），
    worker 新加一个自己一定过的测试，然后把 check 指向**只跑那个测试**。
    命令是正常的 pytest 调用，vacuous_checks 在语法层看不出问题。
    """
    root = _repo(tmp_path, passing=False)  # 真测试是红的
    (root / "tests" / "test_selfmade.py").write_text(
        "def test_worker_says_ok():\n    assert True\n", encoding="utf-8")
    cmd = f"{PYTEST_CMD} tests/test_selfmade.py -q"
    assert _sh(root, cmd) == 0, "前提：这条恒真自测自己是绿的"

    rep = _dispatch_worker_checks(tmp_path, root,
                                 changed=("tests/test_selfmade.py", "src/a.py"),
                                 check_cmd=cmd)
    assert rep.outcome.value != "merged", f"恒真自测合并了：{rep}"


def test_the_send_back_reason_says_the_check_was_worker_written(tmp_path):
    """打回理由要说清「这条是你自己写的所以全量验」。

    原来的文案固定拼「这一轮改过：<裁判权文件>」，worker 来源时那个列表是
    空的，会打出「这一轮改过：」后跟一片空白 —— 那种提示比不给更糟，
    worker 下一轮不知道该改什么。
    """
    root = _repo(tmp_path, passing=False)
    (root / "tests" / "test_selfmade.py").write_text(
        "def test_worker_says_ok():\n    assert True\n", encoding="utf-8")
    cmd = f"{PYTEST_CMD} tests/test_selfmade.py -q"

    rep = _dispatch_worker_checks(tmp_path, root,
                                 changed=("tests/test_selfmade.py",),
                                 check_cmd=cmd)
    assert "worker" in rep.escalation_reason, rep.escalation_reason
    assert "这一轮改过：\n" not in rep.escalation_reason
    assert not rep.escalation_reason.rstrip().endswith("这一轮改过："), \
        f"空列表拼进了理由：{rep.escalation_reason!r}"


def test_human_written_checks_keep_the_sampling(tmp_path):
    """反向守边界：人写的 check 仍然抽样，没被这次改动顺手变成全量。

    人有权写一条看起来无聊的命令，而且全量验会给每一轮都加一次 pytest
    的开销。这条和 test_the_probe_does_not_run_when_no_judge_file_changed
    是同一个约束的两面，留在这里是为了让 F3 的改动范围一眼可见。
    """
    root = _repo(tmp_path)
    (root / "tests" / "test_b.py").write_text("def test_y(): pass\n",
                                              encoding="utf-8")
    seen: list[str] = []
    _dispatch(tmp_path, root, changed=("tests/test_b.py",), probes=seen)
    assert seen == [], f"人写的 check 被改成全量探针了：{seen}"
