"""影子代码：被 .gitignore 挡住、四道闸门都看不见的代码文件。

这个洞的形状不是「某个组件判错了」，而是**四道闸门共用一个视野，
而那个视野是 git 的，不是文件系统的**。后分级、范围监工、runbook、
架构监工全部吃 changed_paths，而 changed_paths 来自 git diff。

实测构造（见 shadow_code 的 docstring）：
    worker 改 src/app.py 加一行 `import build.hook`，同时新建 build/hook.py
    → 监工看得见那行 import，看不见被 import 的东西
    → check 命令**真的执行了** build/hook.py

最要紧的一条不变量：这道检测在干净仓库上必须一声不响。
每次都响的闸门等于没有闸门（和漂移熔断器同一条教训）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factory.harness.workspace import shadow_code


def _repo(tmp_path: Path, ignore: str = "build/\n") -> Path:
    root = tmp_path / "wp"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(ignore, encoding="utf-8")
    g = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True)
    g("init", "-q")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return root


def test_a_clean_repo_reports_nothing(tmp_path):
    """干净仓库零命中。这条比「能抓到」更重要 —— 见模块 docstring。"""
    assert shadow_code(_repo(tmp_path)) == ()


def test_this_repo_itself_reports_nothing():
    """在真仓库上零命中（.venv 里有 724 个 .py，实测过）。

    单测里的假仓库不含依赖目录，所以过滤逻辑对不对只有这条能验。

    这条曾经真的红过：前端上线后 frontend/dist/assets/index-*.js 是
    vite 的构建产物、被 frontend/.gitignore 挡着，于是每次跑都命中一条。
    修法是在 shadow_code 里排掉打包产物文件名（见 _BUNDLED_ASSET），
    不是把 dist 加进 _VENDOR_DIRS —— dist/ 正是最好藏代码的地方。
    """
    assert shadow_code(Path(".")) == ()


def test_a_new_code_file_under_an_ignored_dir_is_reported(tmp_path):
    root = _repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "hook.py").write_text("print('x')\n", encoding="utf-8")
    assert shadow_code(root) == ("build/hook.py",)


def test_git_itself_does_not_see_it(tmp_path):
    """先证明这个洞真的存在，再证明我们补上了。

    没有这条，上面那个测试只是在测一个函数的返回值，
    读者无从判断它拦的是不是一个真问题。
    """
    root = _repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "hook.py").write_text("print('x')\n", encoding="utf-8")

    from factory.harness.workspace import capture_diff
    diff, paths = capture_diff(root)
    assert "build/hook.py" not in paths, "git diff 就是看不见它"
    assert "hook.py" not in diff
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True).stdout
    assert "build" not in status, "git status --porcelain 也不报"
    assert shadow_code(root) == ("build/hook.py",)


# --- 噪音抑制：这道检测能不能用，全看这几条 ---------------------------------

def test_pyc_and_pycache_are_not_reported(tmp_path):
    """__pycache__/*.pyc 是 check 命令自己产生的，每跑一次就有。"""
    root = _repo(tmp_path, ignore="__pycache__/\n*.pyc\n")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00")
    assert shadow_code(root) == ()


def test_dependency_dirs_are_not_reported(tmp_path):
    """.venv / node_modules 里的 .py 不是 worker 写的。"""
    root = _repo(tmp_path, ignore=".venv/\nnode_modules/\n")
    for d, f in ((".venv/lib/site-packages/pytest", "main.py"),
                 ("node_modules/left-pad", "index.js")):
        (root / d).mkdir(parents=True)
        (root / d / f).write_text("whatever\n", encoding="utf-8")
    assert shadow_code(root) == ()


def test_non_code_files_are_not_reported(tmp_path):
    """.log / .json 之类不进：它们不会被 import 执行。

    这是刻意收窄的范围，不是遗漏 —— 一份 ignored 的 config.json 也可能影响
    check 结果，但把非代码也算进来就会把每个仓库的日志目录全报出来，
    回到「每次都响」。要扩范围就扩 _CODE_SUFFIXES，而那是共用的一张表。
    """
    root = _repo(tmp_path, ignore="logs/\n")
    (root / "logs").mkdir()
    (root / "logs" / "run.log").write_text("x\n", encoding="utf-8")
    (root / "logs" / "cfg.json").write_text("{}\n", encoding="utf-8")
    assert shadow_code(root) == ()


def test_build_is_checked_even_though_skip_dirs_contains_it(tmp_path):
    """两张词表刻意不合并 —— 合了就等于给攻击开白名单。

    _SKIP_DIRS 里有 build/（给架构监工找邻居代码时跳过生成物），
    _VENDOR_DIRS 里**没有** build/。语义相反：一个说「这里没有值得读的代码」，
    一个要答「这里有不该出现的代码」。build/ 恰好既是常见生成物目录，
    又是最好藏代码的地方。
    """
    from factory.harness.workspace import _SKIP_DIRS, _VENDOR_DIRS
    assert "build" in _SKIP_DIRS
    assert "build" not in _VENDOR_DIRS
    assert "dist" not in _VENDOR_DIRS

    root = _repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "hook.py").write_text("print('x')\n", encoding="utf-8")
    assert shadow_code(root) == ("build/hook.py",)


def test_hashed_bundles_are_not_reported_but_handwritten_dist_code_is(tmp_path):
    """打包产物放过，同一个目录里手写的代码照抓。

    这两条必须一起验。只留前半条，等于给 dist/ 开了目录级白名单 ——
    而 dist/ 正是最好藏代码的地方（见上一条测试）。
    """
    root = _repo(tmp_path, ignore="dist/\n")
    (root / "dist" / "assets").mkdir(parents=True)
    for name in ("index-DK7trwX8.js", "vendor.a1b2c3d4e5.js", "app-9f8e7d6c.css"):
        (root / "dist" / "assets" / name).write_text("//x\n", encoding="utf-8")
    assert shadow_code(root) == (), "vite/webpack 的哈希产物不该报"

    (root / "dist" / "hook.py").write_text("print('x')\n", encoding="utf-8")
    (root / "dist" / "sneaky.js").write_text("//x\n", encoding="utf-8")
    assert shadow_code(root) == ("dist/hook.py", "dist/sneaky.js")


def test_an_untracked_but_not_ignored_file_is_not_shadow(tmp_path):
    """没被 ignore 的新文件不算影子 —— `add -A -N` 会把它带进 diff。

    这条划清边界：影子的定义是「闸门看不见」，不是「还没提交」。
    """
    root = _repo(tmp_path)
    (root / "src" / "new.py").write_text("y = 2\n", encoding="utf-8")

    from factory.harness.workspace import capture_diff
    _, paths = capture_diff(root)
    assert "src/new.py" in paths, "它本来就在闸门视野里"
    assert shadow_code(root) == ()


# --- 接线：函数对不对和它有没有接上是两件事 ---------------------------------

def _dispatch(tmp_path, root, *, checks_ran):
    """真跑一次 dispatcher，记录 check 有没有被调。"""
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class Recording:
        """回归监工替身：被调到就记一笔。"""
        from factory.audit.models import SupervisorRole as _R
        role = _R.REGRESSION

        def review(self, workspace, checks):
            from factory.audit.models import Verdict
            from factory.supervisors.base import SupervisorReport
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(task_id="T-shadow", prompt="改点东西",
                declared_paths=("src/app.py",),
                checks=(CheckSpec(name="ok", command="true"),))
    d = Dispatcher(adapter=FakeAdapter([_result(paths=("src/app.py",))]),
                   store=AuditStore(tmp_path / "a.db"),
                   supervisor=Recording())
    return d.run(task, root)


def test_shadow_code_blocks_the_merge(tmp_path):
    """影子文件在场时不许 merge。这是接线测试，不是行为测试。"""
    root = _repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "hook.py").write_text("print('x')\n", encoding="utf-8")

    ran: list = []
    rep = _dispatch(tmp_path, root, checks_ran=ran)
    assert rep.outcome.value != "merged", f"影子代码在场却合并了：{rep}"
    assert "hook.py" in rep.escalation_reason


def test_checks_never_run_on_a_contaminated_tree(tmp_path):
    """必须拦在 check **之前**。

    这条不是优化。check 全绿是四道闸门放行的唯一依据，而 check 跑在真实文件树
    上、会 import 执行影子文件（实测打印出来过）。先跑 check 再拦，等于先让
    未审代码执行一遍、再宣布这次不算 —— 而它已经执行了。
    """
    root = _repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "hook.py").write_text("print('x')\n", encoding="utf-8")

    ran: list = []
    _dispatch(tmp_path, root, checks_ran=ran)
    assert ran == [], f"check 在被污染的树上跑了：{ran}"


def test_a_clean_tree_still_merges(tmp_path):
    """没有影子文件时一切照常 —— 误拦一切等于拦不住任何东西。"""
    root = _repo(tmp_path)
    ran: list = []
    rep = _dispatch(tmp_path, root, checks_ran=ran)
    assert ran, "干净树上 check 该照常跑"
    assert rep.outcome.value == "merged", f"干净树被误拦：{rep}"
