"""端到端 smoke：真的调 claude。默认不跑。

    uv run pytest -m smoke -s

这是 spec §9 P0 的完成判据：一个 A 类任务端到端跑通，审计记录字段完整。
"""
import shutil
import subprocess

import pytest

from factory.audit.store import AuditStore
from factory.cli import main


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.mark.smoke
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI 不可用")
def test_class_a_task_end_to_end(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("# scratch\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")

    task = tmp_path / "task.yaml"
    task.write_text(
        "task_id: T-smoke-1\n"
        "prompt: |\n"
        "  在仓库根目录创建 greet.py，实现 greet(name) -> str，\n"
        "  返回 \"Hello, {name}!\"。不要改动其他文件。\n"
        "spec_ref: [AC-1]\n"
        "declared_paths: ['greet.py']\n"
        "max_rounds: 2\n"
        "checks:\n"
        "  - name: greet-exists\n"
        "    command: test -f greet.py\n"
        "  - name: greet-output\n"
        "    command: python -c \"import greet; print(greet.greet('world'))\"\n"
        "    expect: stdout_contains\n"
        "    value: 'Hello, world!'\n",
        encoding="utf-8",
    )

    db = tmp_path / "audit.db"
    # --worktree：不加的话 workspace 就是这个仓库本身，而 land() 会（正确地）
    # 拒绝在主工作树提交 —— 于是 commit 字段永远是 None，这个判据就覆盖不到
    # spec §5 的最后一个字段。P0 核对表当年漏掉 commit，也是同一个原因。
    wt_root = tmp_path / "wts"
    code = main(["run", str(task), "--workspace", str(ws), "--db", str(db),
                 "--worktree", "--worktree-root", str(wt_root),
                 "--max-turns", "20"])
    assert code == 0, "A 类任务应当自动 merge"

    # 审计记录字段完整性 —— 这是完成判据的后半句
    #
    # 取**最后一次** attempt，不是 get(1)。max_rounds=2 意味着模型第一轮写错、
    # 第二轮改对是完全正常的一次 merge：那时 attempt 1 的 resolution 是
    # reworked，assert ... == "merged" 会挂。写死 get(1) 等于在断言
    # 「模型一次就写对」—— 那是模型的运气，不是工厂的行为，而这个测试
    # 是 P0 的完成判据，判的必须是后者。
    #
    # 这条曾经真的间歇性失败过（同一份代码，全量跑挂、单独跑过，用了 2 轮）。
    # 一个分不清「真的坏了」和「模型这次多用了一轮」的判据，
    # 在无人夜跑里等于没有判据。
    attempts = AuditStore(db).attempts_for("T-smoke-1")
    assert attempts, "至少要有一次 attempt"
    row = attempts[-1]
    assert row.task_id == "T-smoke-1"
    assert row.attempt_no == len(attempts)
    assert row.spec_ref == ["AC-1"]
    assert row.oracle_class == "A"
    assert row.class_reason
    assert row.harness == "claude_code"
    assert row.harness_version and row.harness_version != "unknown"
    # 模型按 A 类的重试阶梯 [haiku, sonnet, opus] 走，第几轮就是第几档。
    # 写死 "haiku" 是同一个 get(1) 假设的第二处实例：只在一轮就过时成立。
    assert row.model == ("haiku", "sonnet", "opus")[min(row.attempt_no - 1, 2)]
    assert row.diff_hash and len(row.diff_hash) == 64
    assert row.transcript_path and row.transcript_path.endswith(".jsonl")
    assert row.tokens_in > 0 and row.tokens_out > 0
    assert row.cost_usd > 0
    assert row.wall_clock_ms > 0
    assert row.resolution == "merged"
    assert row.linked_defects == []
    assert row.created_at is not None
    roles = {v.role for v in row.supervisors}
    assert {"regression", "risk"} <= roles

    # spec §5 的最后一个字段：commit。产出在任务分支上，**主工作树没被动过**。
    assert row.commit and len(row.commit) == 40
    wt = wt_root / "T-smoke-1"
    assert (wt / "greet.py").exists(), "产出在 worktree 里，不在主工作树"
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt,
                          capture_output=True, text=True).stdout.strip()
    assert head == row.commit
    # 提交的必须正好是监工审过的那一组 —— check 跑 `python -c "import greet"`
    # 会留下 __pycache__，`add -A` 会把它一起提交，那时 commit 和 diff_hash
    # 就描述不同的内容了。这个错配是真跑抓到的，所以判据放在真跑里。
    names = subprocess.run(
        ["git", "show", "--name-only", "--format=", row.commit], cwd=wt,
        capture_output=True, text=True).stdout.split()
    assert names == ["greet.py"], f"提交了监工没审过的东西：{names}"
    assert not (ws / "greet.py").exists(), "主工作树不该被写入"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=ws,
                          capture_output=True, text=True).stdout.strip() == ""
    # 回查那一跳：git blame 给短 sha，defect 要按它找回 attempt
    assert AuditStore(db).attempt_by_commit(row.commit[:8]).id == row.id

    # 用了多轮的话，前几轮也该有完整记录 —— 打回的那一轮同样是审计现场。
    # 只查最后一轮会让「第一轮的 diff_hash 没写进去」这类缺陷躲过判据。
    for prev in attempts[:-1]:
        assert prev.resolution == "reworked", (
            f"attempt #{prev.attempt_no} 不是最后一轮，"
            f"resolution 应当是 reworked，实际 {prev.resolution}")
        # 打回必须有理由：至少一个监工判了 fail，否则这一轮为什么重跑无从解释。
        fails = [v for v in prev.supervisors if v.verdict == "fail"]
        assert fails, f"attempt #{prev.attempt_no} 被打回却没有 fail 裁决"

        # **一次没拿到模型回复的 attempt 没有 diff 也没有账单，这是对的。**
        # 真跑抓到的（2026-08-08）：attempt #1 撞上 `API Error: 400 Upstream
        # request failed`，3.4s 就回来了，diff_hash=None、cost=$0、tokens=0，
        # 然后 #2 正常 merge。原来这里无条件要求 diff_hash 和 cost>0，于是
        # 一个**处理得完全正确**的上游故障把 P0 判据判成了失败。
        #
        # 判据要钉的是「工厂有没有丢现场」，不是「上游有没有抖」。所以分两支：
        # 跑起来了的必须留全套现场；根本没跑起来的只要求那条失败原因在库里
        # 说得清（否则第二天没人知道为什么重跑）。
        launched = prev.tokens_in > 0
        if launched:
            assert prev.diff_hash and len(prev.diff_hash) == 64
            assert prev.cost_usd > 0
        else:
            assert prev.diff_hash is None and prev.cost_usd == 0.0, (
                "没跑起来的 attempt 不该有 diff 或账单")
            assert any("api_error" in str(c.get("got", "")).lower()
                       or "error" in str(c.get("got", "")).lower()
                       for v in fails for c in (v.claims or ())), \
                "attempt 没跑起来，但审计里查不到失败原因"
