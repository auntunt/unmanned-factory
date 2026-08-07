"""ShellAdapter 测试。

重点不是「shell 能跑」，而是**Protocol 真的可替换**：同一个 Dispatcher、
同一批监工、同一张审计表，换掉 adapter 后全链路照样跑通。
所以这里有一个用 ShellAdapter 跑完整 dispatcher 的端到端测试。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from factory.audit.store import AuditStore
from factory.cli import main
from factory.dispatcher import Dispatcher, Outcome
from factory.harness.base import ExitStatus, Limits
from factory.harness.shell import MODEL_ENV, PROMPT_ENV, TASK_ENV, ShellAdapter
from factory.task import CheckSpec, Task


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=ws, check=True,
                   capture_output=True)
    return ws


def _script(tmp_path, name: str, body: str) -> str:
    """写一个可执行 python 脚本。常用模块预先 import 好，body 直接用。"""
    p = tmp_path / name
    p.write_text(
        "#!/usr/bin/env python3\nimport os, pathlib, sys\n" + body + "\n",
        encoding="utf-8",
    )
    p.chmod(p.stat().st_mode | 0o111)
    return str(p)


def _task(**kw) -> Task:
    return Task(
        task_id=kw.pop("task_id", "T-sh"),
        prompt=kw.pop("prompt", "make out.py"),
        **kw,
    )


# ── 基本契约 ──────────────────────────────────────────────────────────────

def test_writes_file_and_captures_diff(tmp_path, repo):
    sh = _script(tmp_path, "w.py", "open('out.py','w').write('X=1\\n')\n")
    result = ShellAdapter([sh]).run(_task(), repo, Limits())
    assert result.ok
    assert result.changed_paths == ("out.py",)
    assert result.diff_hash is not None


def test_nonzero_exit_is_error(tmp_path, repo):
    """和 claude 相反：这里退出码就是真相。"""
    sh = _script(tmp_path, "f.py", "import sys; sys.exit(3)\n")
    result = ShellAdapter([sh]).run(_task(), repo, Limits())
    assert not result.ok
    assert result.exit_status is ExitStatus.ERROR
    assert "exit 3" in result.error_text


def test_diff_captured_even_on_failure(tmp_path, repo):
    """失败的 attempt 也可能留下改动，必须入审计。"""
    sh = _script(
        tmp_path, "pf.py",
        "open('half.py','w').write('partial\\n')\nimport sys; sys.exit(1)\n",
    )
    result = ShellAdapter([sh]).run(_task(), repo, Limits())
    assert not result.ok
    assert result.changed_paths == ("half.py",)


def test_custom_ok_exit_codes(tmp_path, repo):
    sh = _script(tmp_path, "e.py", "import sys; sys.exit(2)\n")
    ok = ShellAdapter([sh], exit_code_ok=(0, 2)).run(_task(), repo, Limits())
    assert ok.ok


def test_timeout_reported_as_timeout(tmp_path, repo):
    sh = _script(tmp_path, "slow.py", "import time; time.sleep(30)\n")
    result = ShellAdapter([sh]).run(_task(), repo, Limits(timeout_s=1))
    assert result.exit_status is ExitStatus.TIMEOUT
    assert "timeout" in result.error_text


def test_missing_binary_is_error_not_crash(repo):
    result = ShellAdapter(["/nonexistent/xyz"]).run(_task(), repo, Limits())
    assert result.exit_status is ExitStatus.ERROR
    assert "cannot launch" in result.error_text


# ── version()：真跑发现的 bug，别放松这几条 ─────────────────────────────────

def test_version_does_not_execute_the_worker(tmp_path, repo, monkeypatch):
    """原实现跑 `argv[0] --version` 且没设 cwd，把 worker 在调用方目录跑了一遍。

    结果是测试往编排层自己的仓库写文件并被 git add -A 提交进去（10a0d9f）。
    默认路径必须一次都不执行目标程序。
    """
    sh = _script(tmp_path, "v.py", "open('SIDE_EFFECT','w').write('x')\n")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    ShellAdapter([sh]).version()

    assert not (cwd / "SIDE_EFFECT").exists(), "version() 在调用方 cwd 里执行了 worker"
    assert not (repo / "SIDE_EFFECT").exists()
    assert not (tmp_path / "SIDE_EFFECT").exists()


def test_version_defaults_to_content_hash(tmp_path):
    sh = _script(tmp_path, "h.py", "pass\n")
    v = ShellAdapter([sh]).version()
    assert v.startswith("sha256:"), v


def test_version_hash_changes_when_script_changes(tmp_path):
    """harness_version 的用途就是看出「这次和上次不是同一个 worker」。"""
    sh = Path(_script(tmp_path, "c.py", "pass\n"))
    before = ShellAdapter([str(sh)]).version()
    sh.write_text("#!/usr/bin/env python3\nprint('changed')\n", encoding="utf-8")
    assert ShellAdapter([str(sh)]).version() != before


def test_version_probe_runs_in_a_clean_temp_dir(tmp_path, monkeypatch):
    """显式开探针时，也不能在调用方 cwd 或 workspace 里跑。"""
    sh = _script(
        tmp_path, "p.py",
        "import os, pathlib\n"
        "pathlib.Path('WHERE').write_text(os.getcwd())\n"
        "print('probe 9.9')\n",
    )
    cwd = tmp_path / "here"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    v = ShellAdapter([sh], probe_version=True).version()
    assert v == "probe 9.9"
    assert not (cwd / "WHERE").exists(), "探针跑在了调用方 cwd 里"


def test_version_probe_falls_back_to_hash_on_empty_output(tmp_path):
    """裸脚本不认 --version，输出为空。空 splitlines()[0] 曾经直接 IndexError。"""
    sh = _script(tmp_path, "q.py", "pass\n")
    v = ShellAdapter([sh], probe_version=True).version()
    assert v.startswith("sha256:"), v


def test_version_unknown_for_missing_executable():
    assert ShellAdapter(["/nonexistent/xyz"]).version() == "unknown"


def test_run_does_not_touch_the_caller_cwd(tmp_path, repo, monkeypatch):
    """worker 只应改 workspace。调用方目录必须一个文件都不多。"""
    sh = _script(tmp_path, "w2.py", "open('out.py','w').write('X=1\\n')\n")
    cwd = tmp_path / "caller"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    ShellAdapter([sh]).run(_task(), repo, Limits())

    assert (repo / "out.py").exists()
    assert list(cwd.iterdir()) == [], f"调用方目录被污染：{list(cwd.iterdir())}"


def test_empty_argv_rejected():
    with pytest.raises(ValueError):
        ShellAdapter([])


# ── prompt 送达 ───────────────────────────────────────────────────────────

def test_prompt_slot_replaced_in_argv(tmp_path, repo):
    sh = _script(tmp_path, "a.py", "import sys; open('got.txt','w').write(sys.argv[1])\n")
    ShellAdapter([sh, "{prompt}"]).run(_task(prompt="HELLO-ARGV"), repo, Limits())
    assert (repo / "got.txt").read_text() == "HELLO-ARGV"


def test_prompt_also_available_via_env(tmp_path, repo):
    sh = _script(
        tmp_path, "b.py",
        f"import os; open('got.txt','w').write(os.environ[{PROMPT_ENV!r}])\n",
    )
    ShellAdapter([sh]).run(_task(prompt="HELLO-ENV"), repo, Limits())
    assert (repo / "got.txt").read_text() == "HELLO-ENV"


def test_task_id_and_model_in_env(tmp_path, repo):
    sh = _script(
        tmp_path, "c.py",
        "import os\n"
        f"open('got.txt','w').write(os.environ[{TASK_ENV!r}] + '|' "
        f"+ os.environ.get({MODEL_ENV!r}, '-'))\n",
    )
    ShellAdapter([sh]).run(_task(task_id="T-42"), repo, Limits(), model="haiku")
    assert (repo / "got.txt").read_text() == "T-42|haiku"


def test_workspace_slot_replaced(tmp_path, repo):
    sh = _script(tmp_path, "d.py", "import sys; open('got.txt','w').write(sys.argv[1])\n")
    ShellAdapter([sh, "{workspace}"]).run(_task(), repo, Limits())
    assert (repo / "got.txt").read_text() == str(repo)


def test_prompt_with_shell_metacharacters_is_not_executed(tmp_path, repo):
    """走 argv 不走 shell：反引号和 $() 必须原样送到，不能被求值。

    prompt 是任务文件里的自由文本，将来还会由 PRD 生成器产出。
    这条测试是那个决定的锚点，别删。
    """
    nasty = "fix `touch /tmp/pwned_factory` and $(echo bad)"
    sh = _script(
        tmp_path, "n.py",
        f"import os; open('got.txt','w').write(os.environ[{PROMPT_ENV!r}])\n",
    )
    ShellAdapter([sh]).run(_task(prompt=nasty), repo, Limits())
    assert (repo / "got.txt").read_text() == nasty
    assert not Path("/tmp/pwned_factory").exists()


# ── Protocol 可替换性（本文件的真正目的）────────────────────────────────────

def test_dispatcher_runs_end_to_end_with_shell_adapter(tmp_path, repo):
    """换 adapter，Dispatcher / 监工 / 审计表一行都不用改。"""
    sh = _script(tmp_path, "good.py", "open('out.py','w').write('X=1\\n')\n")
    store = AuditStore(":memory:")
    d = Dispatcher(adapter=ShellAdapter([sh], name="codemod"), store=store)

    task = _task(
        declared_paths=("out.py",),
        checks=(CheckSpec(name="exists", command="test -f out.py"),),
    )
    report = d.run(task, repo)
    assert report.outcome is Outcome.MERGED

    row = store.get(report.attempt_ids[0])
    assert row.harness == "codemod", "name 要能落审计，否则没法按 harness 比命中率"
    assert row.resolution == "merged"
    assert row.cost_usd == 0.0, "脚本没有 token 消耗，$0 是真话不是缺省值"


def test_shell_adapter_task_reworks_then_escalates(tmp_path, repo):
    """脚本一直不满足 check → 三轮打回后升级，和 claude 路径同样的收口。"""
    sh = _script(tmp_path, "bad.py", "open('wrong.py','w').write('nope\\n')\n")
    store = AuditStore(":memory:")
    d = Dispatcher(adapter=ShellAdapter([sh]), store=store)

    task = _task(
        declared_paths=("out.py",),
        checks=(CheckSpec(name="exists", command="test -f out.py"),),
    )
    report = d.run(task, repo)
    assert report.outcome is Outcome.ESCALATED
    assert report.rounds == 3
    resolutions = [store.get(a).resolution for a in report.attempt_ids]
    assert resolutions == ["reworked", "reworked", "escalated"]


def test_shell_adapter_respects_d_class_hard_gate(tmp_path, repo):
    """硬闸门不认 harness —— 换 adapter 不能成为绕过 D 类的路径。"""
    sh = _script(tmp_path, "never.py", "open('SHOULD_NOT_EXIST','w').write('x')\n")
    store = AuditStore(":memory:")
    d = Dispatcher(adapter=ShellAdapter([sh]), store=store)

    task = _task(declared_ops=("prod_deploy",))
    report = d.run(task, repo)
    assert report.outcome is Outcome.BLOCKED_HARD_GATE
    assert not (repo / "SHOULD_NOT_EXIST").exists(), "D 类下脚本一次都不该被执行"


# ── CLI 集成：--harness shell 真的换掉了 worker ─────────────────────────

def _repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=ws, check=True,
                   capture_output=True)
    return ws


def test_cli_harness_shell_records_shell_in_audit(tmp_path, capsys):
    """审计表的 harness 字段必须是 shell，否则按 harness 比命中率就无从下手。"""
    ws = _repo(tmp_path)
    worker = _script(tmp_path, "w.py", "pathlib.Path('gen.py').write_text('X=1\\n')")
    task = tmp_path / "t.yaml"
    task.write_text(
        yaml.safe_dump(
            {"task_id": "T-sh-cli", "prompt": "generate",
             "declared_paths": ["gen.py"],
             "checks": [{"name": "exists", "command": "test -f gen.py"}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    db = tmp_path / "a.db"
    code = main(["run", str(task), "--workspace", str(ws), "--db", str(db),
                 "--harness", "shell", "--shell-argv", worker])
    assert code == 0
    assert "merged" in capsys.readouterr().out

    row = AuditStore(db).get(1)
    assert row.harness == "shell"
    assert row.resolution == "merged"
    # 脚本没有 token 消耗，这是真话不是缺实现
    assert row.cost_usd == 0.0
    assert row.tokens_in == 0


def test_cli_harness_shell_without_argv_is_rejected(tmp_path, capsys):
    """缺 --shell-argv 要当场报错，不能默默退回 claude —— 那等于偷偷换了 worker。"""
    ws = _repo(tmp_path)
    task = tmp_path / "t.yaml"
    task.write_text(
        yaml.safe_dump({"task_id": "T-sh-bad", "prompt": "x",
                        "checks": [{"name": "n", "command": "true"}]},
                       allow_unicode=True),
        encoding="utf-8",
    )
    code = main(["run", str(task), "--workspace", str(ws),
                 "--db", str(tmp_path / "a.db"), "--harness", "shell"])
    assert code == 2
    assert "shell-argv" in capsys.readouterr().err


def test_cli_shell_argv_passes_prompt_slot(tmp_path):
    """{prompt} 占位符经 CLI 一路传到脚本里。"""
    ws = _repo(tmp_path)
    worker = _script(
        tmp_path, "w.py",
        "pathlib.Path('got.txt').write_text(sys.argv[1])",
    )
    task = tmp_path / "t.yaml"
    task.write_text(
        yaml.safe_dump(
            {"task_id": "T-sh-slot", "prompt": "写一个 slug 函数",
             "declared_paths": ["got.txt"],
             "checks": [{"name": "exists", "command": "test -f got.txt"}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    main(["run", str(task), "--workspace", str(ws), "--db", str(tmp_path / "a.db"),
          "--harness", "shell", "--shell-argv", worker, "--shell-argv", "{prompt}"])
    assert (ws / "got.txt").read_text(encoding="utf-8") == "写一个 slug 函数"
