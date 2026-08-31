"""worker 给自己出卷子：新增 runner 会自动加载的配置文件。

洞的形状和影子代码不同：conftest.py **在** changed_paths 里，闸门看得见它，
只是没人管。三道闸门各有各的不管的理由：

  范围监工  declared_paths 为空时一律 PASS，而口述来源的任务绝大多数为空
  后分级    conftest.py 不匹配任何分级规则 → A 类
  runbook   规则查的是文件**内容**里的关键词，而危害在于「这个文件出现了」

这是 runbook「项目规则不许放在 workspace 里」的同一条道理 —— 那条封的是
我们自己读的规则文件，这条封的是 runner 自己会去找的文件。后者我们没在读，
所以漏了。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.workspace import added_paths, runner_hooks
from tests.conftest import PY, git_setup


def _repo(tmp_path: Path, *, with_conftest: bool = False) -> Path:
    root = tmp_path / "wp"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_real.py").write_text(
        "def test_important():\n    assert 1 == 2\n", encoding="utf-8")
    if with_conftest:
        (root / "tests" / "conftest.py").write_text("", encoding="utf-8")
    git_setup(root)
    return root


# 把失败改判成通过。**不是** items.clear()：清空收集给的退出码是 5
# （no tests collected），不是 0 —— 第一次测量时我读的是 `tail` 的 $?，
# 量错了。真正能拿到退出码 0 的是改判裁决这一手。
_FAKE_GREEN = """import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    if rep.outcome == "failed":
        rep.outcome = "passed"
        rep.longrepr = None
"""


def test_the_hole_is_real_a_conftest_makes_a_failing_test_pass(tmp_path):
    """先证明洞存在：清空收集的 conftest 让 `pytest -q` 退出码变 0。

    没有这条，下面那些测试只是在测一个函数的返回值。
    """
    root = _repo(tmp_path)
    red = subprocess.run([PY, "-m", "pytest", "-q"], cwd=root,
                         capture_output=True, text=True)
    assert red.returncode != 0, f"基线该是红的，stderr={red.stderr!r}"

    (root / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")
    green = subprocess.run([PY, "-m", "pytest", "-q"], cwd=root,
                           capture_output=True, text=True)
    # 带上 stderr：这条断言最常见的假红原因是 pytest 压根没起来（解释器不对），
    # 而不是洞真被堵了。光看 returncode 分不出这两种情况。
    assert green.returncode == 0, f"洞不存在了？stderr={green.stderr!r}"
    assert "passed" in green.stdout


def test_a_new_conftest_is_reported(tmp_path):
    root = _repo(tmp_path)
    (root / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")
    assert runner_hooks(root) == ("conftest.py",)


def test_an_existing_conftest_is_not_reported(tmp_path):
    """只判**新增**。项目里本来就有 conftest.py 完全正常（本仓库就有），
    改它也可能是任务的正当内容。"""
    root = _repo(tmp_path, with_conftest=True)
    (root / "tests" / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")
    assert runner_hooks(root) == ()
    assert added_paths(root) == (), "它是修改不是新增"


def test_a_clean_round_reports_nothing(tmp_path):
    root = _repo(tmp_path)
    (root / "tests" / "test_new.py").write_text("def test_x(): pass\n",
                                                encoding="utf-8")
    assert runner_hooks(root) == ()


def test_this_repo_itself_reports_nothing():
    assert runner_hooks(Path(".")) == ()


def test_pycache_from_the_check_run_is_not_reported(tmp_path):
    """check 命令自己产生的 .pyc 每跑一次就新增一批 —— 全都是新增文件。"""
    root = _repo(tmp_path)
    (root / "tests" / "__pycache__").mkdir()
    (root / "tests" / "__pycache__" / "test_real.pyc").write_bytes(b"\x00")
    assert runner_hooks(root) == ()


def test_legitimate_config_files_are_not_reported(tmp_path):
    """pyproject.toml / package.json / Makefile 刻意不在词表里。

    它们能影响构建，但任务合法要改它们（加依赖、加脚本）。列进来会让这道检测
    在正常任务上响 —— 判据是「本来不存在、runner 会自己去找它」，
    不是「能影响构建」。每次都响的闸门等于没有闸门。
    """
    from factory.harness.workspace import _RUNNER_HOOKS
    for f in ("pyproject.toml", "package.json", "Makefile", "setup.cfg"):
        assert f not in _RUNNER_HOOKS, f"{f} 不该在词表里"

    root = _repo(tmp_path)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "Makefile").write_text("all:\n\techo ok\n", encoding="utf-8")
    assert runner_hooks(root) == ()


def test_a_hook_in_a_subdirectory_is_reported(tmp_path):
    """conftest.py 放深一层照样生效，所以判的是文件名不是完整路径。"""
    root = _repo(tmp_path)
    (root / "tests" / "sub").mkdir()
    (root / "tests" / "sub" / "conftest.py").write_text(_FAKE_GREEN,
                                                        encoding="utf-8")
    assert runner_hooks(root) == ("tests/sub/conftest.py",)


def test_added_paths_needs_no_prior_capture_diff(tmp_path):
    """没人先跑过 capture_diff 时，added_paths 自己也能看见未追踪文件。

    最早那版靠 `diff HEAD --diff-filter=A` 而没有先 `add -A -N`，未追踪文件
    根本不在 `git diff HEAD` 里 → 永远返回空。而在 dispatcher 里恰好
    capture_diff 先跑过，所以那里看不出来 —— 「函数依赖调用者先做过某件事」
    是最难查的那种巧合。

    现在的实现不再写索引（见 workspace.added_paths），这条测的是并集里
    `ls-files --others` 那一半：它是**没人跑过 capture_diff** 时唯一的视野。
    配对的那条测反面（capture_diff 先跑过）在文件末尾，两条必须都在 ——
    只留任何一条，都有一个半瞎的实现能全绿。
    """
    root = _repo(tmp_path)
    (root / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")
    # 刻意不调 capture_diff
    assert added_paths(root) == ("conftest.py",)
    assert runner_hooks(root) == ("conftest.py",)


# --- 接线 ---------------------------------------------------------------

def _dispatch(tmp_path, root, checks_ran):
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(task_id="T-hook", prompt="改点东西",
                checks=(CheckSpec(name="ok", command="true"),))
    d = Dispatcher(adapter=FakeAdapter([_result(paths=("tests/test_real.py",))]),
                   store=AuditStore(tmp_path / "a.db"),
                   supervisor=Recording())
    return d.run(task, root)


def test_a_new_conftest_blocks_the_merge(tmp_path):
    """注意 declared_paths 是空的 —— 范围监工在这种任务上一律 PASS，
    而口述来源的任务绝大多数为空。所以这条只能靠新闸门拦。"""
    root = _repo(tmp_path)
    (root / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")
    ran: list = []
    rep = _dispatch(tmp_path, root, ran)
    assert rep.outcome.value != "merged", f"自己出卷子却合并了：{rep}"
    assert "conftest.py" in rep.escalation_reason


def test_checks_do_not_run_when_a_hook_was_added(tmp_path):
    """必须拦在 check 之前：这类文件的作用就是改变 check 的行为。

    先跑 check 再拦，拿到的那份绿本身就是被这个文件伪造的。
    """
    root = _repo(tmp_path)
    (root / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")
    ran: list = []
    _dispatch(tmp_path, root, ran)
    assert ran == [], f"check 在被 hook 的树上跑了：{ran}"


def test_the_two_claims_are_distinct(tmp_path):
    """影子代码和 runner hook 分成两条 claim，不合并。

    worker 要做的事不一样：影子代码是「把文件挪进版本控制或删掉」，
    runner hook 是「别自己出卷子」。糊成一条会让打回指令变模糊，白烧一轮。
    """
    root = _repo(tmp_path)
    (root / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")
    rep = _dispatch(tmp_path, root, [])
    assert "runner-hook-added" in rep.escalation_reason
    assert "shadow-code" not in rep.escalation_reason


def test_a_clean_tree_still_merges(tmp_path):
    root = _repo(tmp_path)
    (root / "tests" / "test_new.py").write_text("def test_x(): pass\n",
                                                encoding="utf-8")
    ran: list = []
    rep = _dispatch(tmp_path, root, ran)
    assert ran, "干净树上 check 该照常跑"
    assert rep.outcome.value == "merged", f"干净树被误拦：{rep}"


# --- added_paths 的只读性与调用顺序 -------------------------------------
#
# 这两条是一次真实的自我纠错留下的。把 added_paths 从「add -A -N 再 diff」
# 改成只读实现时，第一版用了 `diff --cached HEAD --diff-filter=A` 加
# `ls-files --others`。九种形状实测和旧实现同解，离线全套也全绿 —— 但它在
# **真实调用顺序**下静默失效：dispatcher 里 capture_diff 跑在前面，它一
# `add -A -N`，新文件就成了 intent-to-add，从此既不在 `--others` 里（已进
# 索引），也不在 `--cached` 的新增里（git 不把 intent-to-add 算作已 stage
# 的新增）。两条查询之间恰好有一道缝，闸门返回 () —— 和「这一轮很干净」
# 长得一模一样。
#
# 当时套件里只有反向那条测试（`..._needs_no_prior_capture_diff`），没有
# 正向的，所以这个洞在测试面前是隐形的。


def test_added_paths_still_sees_new_files_after_capture_diff(tmp_path):
    """dispatcher 的真实顺序：capture_diff 先跑，added_paths 后跑。

    capture_diff 会把未追踪文件挂成 intent-to-add。这条固定住「挂成
    intent-to-add 之后照样看得见」—— 少了它，任何只查 `--others` 或只查
    `--cached` 的实现都能骗过整套测试，而闸门在生产里已经瞎了。
    """
    from factory.harness.workspace import capture_diff

    root = _repo(tmp_path)
    (root / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")

    capture_diff(root)  # 就是这一步把它变成 intent-to-add
    staged = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True).stdout
    assert " A conftest.py" in staged, f"前提：它已是 intent-to-add: {staged!r}"

    assert added_paths(root) == ("conftest.py",), "intent-to-add 之后瞎了"
    assert runner_hooks(root) == ("conftest.py",), "闸门跟着瞎了"


def test_added_paths_does_not_write_to_the_index(tmp_path):
    """一个名字像检查员的函数不许写它检查的那个仓库。

    旧实现跑 `add -A -N`，症状出在自己身上：套件里那条合法的
    `runner_hooks(Path("."))` 让每次跑离线全套都把开发者真仓库的未追踪文件
    挂上 intent-to-add，`git status` 从 `??` 变成 ` A`。本会话真的差点因此
    把一个临时变异脚本提交进去。
    """
    root = _repo(tmp_path)
    (root / "conftest.py").write_text(_FAKE_GREEN, encoding="utf-8")

    def status() -> str:
        return subprocess.run(["git", "status", "--porcelain"], cwd=root,
                              capture_output=True, text=True).stdout

    before = status()
    assert "?? conftest.py" in before, f"前提：还是未追踪: {before!r}"

    assert added_paths(root) == ("conftest.py",)

    assert status() == before, f"它写了索引：{before!r} → {status()!r}"
