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
    CANARY_NAME,
    applies_to,
    canary_target,
    judge_files_touched,
    probe,
)

CMD = "python -m pytest -q"

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
    g = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True)
    g("init", "-q")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return root


def _sh(root: Path, cmd: str) -> int:
    return subprocess.run(cmd, cwd=root, shell=True, capture_output=True).returncode


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
