"""并行派发测试：AuditStore 并发安全 + CLI 多任务路径。

这些测试离线跑（不调模型、不调 claude CLI），用假 adapter。
测的是并发下**审计记录不丢不串**，这是并行派发唯一不能出错的地方 ——
命中率报表和闸门 3 全靠这张表。
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from factory.audit.models import (
    OracleClass,
    Resolution,
    SupervisorRole,
    Verdict,
)
from factory.audit.store import AuditStore
from factory.cli import main


# ── AuditStore 并发 ───────────────────────────────────────────────────────

def _write_batch(db: Path, task_id: str, n: int) -> None:
    store = AuditStore(db)          # 每线程独立 store，模拟并行派发
    for _ in range(n):
        aid = store.open_attempt(
            task_id=task_id, spec_ref=["AC-1"],
            oracle_class=OracleClass.A, class_reason="r",
            harness="fake", harness_version="v1", model="haiku",
        )
        store.record_verdict(
            aid, role=SupervisorRole.RISK, verdict=Verdict.PASS, claims=[]
        )
        store.finalize(aid, Resolution.MERGED)


def test_concurrent_stores_on_same_file_do_not_race_on_create_all(tmp_path):
    """8 线程同时开 store 撞过 'table already exists'（实测）。"""
    db = tmp_path / "a.db"
    errs: list[Exception] = []

    def go(i: int) -> None:
        try:
            AuditStore(db)
        except Exception as e:      # noqa: BLE001
            errs.append(e)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(go, range(8)))
    assert errs == [], f"建表并发失败：{errs[:2]}"


def test_concurrent_writes_lose_no_rows(tmp_path):
    db = tmp_path / "b.db"
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda i: _write_batch(db, f"T-{i}", 8), range(6)))
    rows = AuditStore(db).all_attempts()
    assert len(rows) == 48


def test_concurrent_writes_keep_attempt_no_unique_per_task(tmp_path):
    """attempt_no 是「任务内第几轮」，并发下撞号必须靠唯一约束+重试兜住。"""
    db = tmp_path / "c.db"
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda i: _write_batch(db, f"T-{i}", 8), range(6)))
    rows = AuditStore(db).all_attempts()
    for tid in {r.task_id for r in rows}:
        nos = [r.attempt_no for r in rows if r.task_id == tid]
        assert len(nos) == len(set(nos)), f"{tid} attempt_no 撞号: {sorted(nos)}"


def test_concurrent_same_task_id_still_serialises_attempt_no(tmp_path):
    """同一 task_id 被并发写（不该发生，但要能兜住）也不能出重复轮次号。"""
    db = tmp_path / "d.db"
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda i: _write_batch(db, "T-same", 6), range(4)))
    rows = AuditStore(db).all_attempts(task_id="T-same")
    nos = [r.attempt_no for r in rows]
    assert len(rows) == 24
    assert sorted(nos) == list(range(1, 25))


def test_verdicts_attach_to_the_right_attempt_under_concurrency(tmp_path):
    """裁决串行到别的 attempt 上，命中率就废了 —— 这是最贵的失败模式。"""
    db = tmp_path / "e.db"

    def go(i: int) -> None:
        store = AuditStore(db)
        aid = store.open_attempt(
            task_id=f"T-{i}", spec_ref=[], oracle_class=OracleClass.A,
            class_reason="r", harness="fake", harness_version="v", model="m",
        )
        store.record_verdict(
            aid, role=SupervisorRole.SPEC, verdict=Verdict.FAIL,
            claims=[{"check": f"only-for-{i}", "command": "",
                     "expected": "x", "got": "y"}],
        )

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(go, range(8)))

    for row in AuditStore(db).all_attempts():
        i = row.task_id.removeprefix("T-")
        checks = [c["check"] for v in row.supervisors for c in v.claims]
        assert checks == [f"only-for-{i}"], f"{row.task_id} 拿到了别人的裁决"


def test_memory_store_still_works(tmp_path):
    """:memory: 不支持 WAL，pragma 分支必须跳过它，否则所有单测都挂。"""
    store = AuditStore(":memory:")
    aid = store.open_attempt(
        task_id="T", spec_ref=[], oracle_class=OracleClass.A,
        class_reason="r", harness="f", harness_version="v", model="m",
    )
    assert store.exists(aid)


def test_wal_mode_enabled_for_file_backed_db(tmp_path):
    db = tmp_path / "f.db"
    store = AuditStore(db)
    with store._session() as s:            # noqa: SLF001 - 探针
        mode = s.execute(__import__("sqlalchemy").text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "wal"


# ── CLI 多任务 / worktree 路径 ─────────────────────────────────────────────

def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")
    return ws


def _fake_claude_cwd(tmp_path):
    """写进**自己的 cwd**，而不是固定路径。

    adapter 用 cwd=workspace 调 harness，所以只有写 cwd 才能验证
    「每个任务落在自己的 worktree 里」。写死路径的假 harness 会让所有
    worktree 看起来都是空的，测试就变成了摆设。
    """
    script = tmp_path / "fake_claude_cwd"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib\n"
        "pathlib.Path('out.py').write_text('X = %r\\n' % os.getcwd())\n"
        "print(json.dumps({'is_error': False, 'session_id': 's',\n"
        "  'total_cost_usd': 0.001, 'duration_ms': 10,\n"
        "  'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | 0o111)
    return str(script)


def _task_file(tmp_path, task_id: str):
    p = tmp_path / f"{task_id}.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "task_id": task_id,
                "prompt": "create out.py",
                "spec_ref": ["AC-1"],
                "declared_paths": ["out.py"],
                "checks": [{"name": "out-exists", "command": "test -f out.py"}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return str(p)


def test_three_tasks_run_in_parallel_and_all_merge(tmp_path, repo, capsys):
    db = tmp_path / "p.db"
    tasks = [_task_file(tmp_path, f"T-{i}") for i in range(3)]
    code = main([
        "run", *tasks, "--workspace", str(repo), "--db", str(db),
        "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(tmp_path / "wt"), "--parallel", "3",
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "3/3 merged" in out


def test_each_task_lands_in_its_own_worktree(tmp_path, repo, capsys):
    """三个任务的 out.py 必须在三个不同目录 —— 这就是隔离本身。"""
    db = tmp_path / "p.db"
    wt_root = tmp_path / "wt"
    tasks = [_task_file(tmp_path, f"T-{i}") for i in range(3)]
    main([
        "run", *tasks, "--workspace", str(repo), "--db", str(db),
        "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(wt_root), "--parallel", "3",
    ])
    capsys.readouterr()

    outs = sorted(wt_root.glob("*/out.py"))
    assert len(outs) == 3, f"应有 3 个独立 out.py，实得 {outs}"
    # 每个文件记录了自己的 cwd，三者必须互不相同
    bodies = {p.read_text(encoding="utf-8") for p in outs}
    assert len(bodies) == 3, "三个任务写进了同一棵树"


def test_parent_repo_stays_clean_after_parallel_run(tmp_path, repo, capsys):
    db = tmp_path / "p.db"
    tasks = [_task_file(tmp_path, f"T-{i}") for i in range(3)]
    main([
        "run", *tasks, "--workspace", str(repo), "--db", str(db),
        "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(tmp_path / "wt"), "--parallel", "3",
    ])
    capsys.readouterr()
    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert status == "", f"父仓库被污染：{status}"


def test_parallel_run_records_every_task_in_audit(tmp_path, repo, capsys):
    db = tmp_path / "p.db"
    tasks = [_task_file(tmp_path, f"T-{i}") for i in range(4)]
    main([
        "run", *tasks, "--workspace", str(repo), "--db", str(db),
        "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(tmp_path / "wt"), "--parallel", "4",
    ])
    capsys.readouterr()
    rows = AuditStore(db).all_attempts()
    assert {r.task_id for r in rows} == {f"T-{i}" for i in range(4)}
    assert all(r.resolution == "merged" for r in rows)


def test_duplicate_task_ids_rejected_before_dispatch(tmp_path, repo, capsys):
    """同 id 并行会撞 attempt_no 也会抢同一个 worktree 目录 —— 提前拒。"""
    db = tmp_path / "p.db"
    t = _task_file(tmp_path, "T-dup")
    code = main([
        "run", t, t, "--workspace", str(repo), "--db", str(db),
        "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(tmp_path / "wt"), "--parallel", "2",
    ])
    assert code == 2
    assert "T-dup" in capsys.readouterr().err


def test_single_task_without_worktree_flag_uses_workspace_directly(
    tmp_path, repo, capsys
):
    """单任务默认不开 worktree：P0 的最便宜路径不能因为 P1 变贵。"""
    db = tmp_path / "p.db"
    wt_root = tmp_path / "wt"
    code = main([
        "run", _task_file(tmp_path, "T-solo"), "--workspace", str(repo),
        "--db", str(db), "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(wt_root),
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    assert not wt_root.exists(), "单任务不该建 worktree 根目录"
    assert (repo / "out.py").exists()


def test_single_task_with_worktree_flag_opts_in(tmp_path, repo, capsys):
    db = tmp_path / "p.db"
    wt_root = tmp_path / "wt"
    code = main([
        "run", _task_file(tmp_path, "T-iso"), "--workspace", str(repo),
        "--db", str(db), "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(wt_root), "--worktree",
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    assert (wt_root / "T-iso" / "out.py").exists()
    assert not (repo / "out.py").exists(), "开了 worktree 就不该碰父仓库工作树"


def test_worktrees_are_kept_for_human_review(tmp_path, repo, capsys):
    """产出没人验收过，不能自动删。"""
    db = tmp_path / "p.db"
    wt_root = tmp_path / "wt"
    main([
        "run", _task_file(tmp_path, "T-keep"), "--workspace", str(repo),
        "--db", str(db), "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(wt_root), "--worktree",
    ])
    out = capsys.readouterr().out
    assert (wt_root / "T-keep").is_dir()
    assert "保留未删" in out


def test_hard_gate_task_in_parallel_batch_never_dispatches(
    tmp_path, repo, capsys
):
    """一批任务里混了 D 类，它必须被拦，且不影响同批其它任务。"""
    db = tmp_path / "p.db"
    d = tmp_path / "d.yaml"
    d.write_text(
        yaml.safe_dump(
            {"task_id": "T-prod", "prompt": "deploy",
             "declared_ops": ["prod_deploy"],
             "checks": [{"name": "noop", "command": "true"}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    code = main([
        "run", _task_file(tmp_path, "T-ok"), str(d),
        "--workspace", str(repo), "--db", str(db),
        "--binary", _fake_claude_cwd(tmp_path),
        "--worktree-root", str(tmp_path / "wt"), "--parallel", "2",
    ])
    out = capsys.readouterr().out
    assert code == 1, out                      # 有任务没 merge
    assert "blocked_hard_gate" in out
    assert "1/2 merged" in out

    rows = {r.task_id: r for r in AuditStore(db).all_attempts()}
    assert rows["T-prod"].harness_version == "n/a", "D 类不该调 adapter"
    assert rows["T-ok"].resolution == "merged"
