"""队列层的原子性保证（C-3 / M-1 / M-2 / M-3 回归测试）。

C-3: _park() 用 `if not exists: replace` 判同名 —— TOCTOU，并发下丢失败证据
M-1: 同一处的后缀循环没有上限
M-2: add() 全量 read_bytes()，指错文件时吃满内存
M-3: .result.json / .claim 非原子写，崩溃留下截断的 JSON
"""

import json
import os
import threading
from pathlib import Path

import pytest

from factory.backlog.store import (
    DONE,
    INBOX,
    MAX_TASK_BYTES,
    NEEDS_HUMAN,
    RUNNING,
    Backlog,
    BacklogError,
    _atomic_write_text,
    _PARK_MAX_SUFFIX,
)

TASK_YAML = """\
id: T-atomicity
goal: 验证原子性
checks:
  - command: "true"
    expect: exit_zero
"""


def task_file(dirpath: Path, name: str = "t.yaml", body: str = TASK_YAML) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text(body, encoding="utf-8")
    return p


def bl(root: Path) -> Backlog:
    return Backlog(root / "queue").ensure()


# ---------- C-3: _park 不能丢文件、不能覆盖 ----------


def test_park_actually_moves_the_file(tmp_path):
    """归档后源目录必须是空的。

    这一条抓的是 os.link + os.replace 的组合陷阱：link 之后 target 和源是
    同一个 inode，此时 rename(2) 是 no-op（POSIX 规定），源文件留在原地。
    症状是任务"归档成功"却还躺在 running/，下一轮 recover 再捞一遍。
    """
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src")))
    q.finish(claim, "merged")

    assert list(q.dir(RUNNING).iterdir()) == [], "running/ 没清空，文件没真搬走"
    landed = list(q.dir(DONE).glob("*.yaml"))
    assert len(landed) == 1, f"done/ 里应该正好一个 yaml，实际 {landed}"
    assert landed[0].read_text(encoding="utf-8") == TASK_YAML, "内容变了"


def test_park_keeps_both_when_name_collides(tmp_path):
    """同名归档两次，两份证据都要留着。

    真实场景：needs-human 里的任务人改完判据重新 add，basename 一样。
    覆盖掉的话第一次的失败证据就没了。
    """
    q = bl(tmp_path)
    for round_no in (1, 2):
        body = TASK_YAML + f"\n# round {round_no}\n"
        claim = q.claim(q.add(task_file(tmp_path / f"src{round_no}", body=body)))
        q.finish(claim, "escalated")

    landed = sorted(q.dir(NEEDS_HUMAN).glob("t*.yaml"))
    assert len(landed) == 2, f"两次归档应留两份，实际 {[p.name for p in landed]}"
    bodies = {p.read_text(encoding="utf-8") for p in landed}
    assert len(bodies) == 2, "两份内容一样 —— 有一份被覆盖了"


def test_concurrent_park_loses_nothing(tmp_path):
    """8 线程并发归档同名任务，磁盘上必须有 8 份。

    C-3 的原始症状：两个线程都看到"不存在"，都算出同一个 T-x.5，
    后者的 os.replace 静默盖掉前者。
    """
    q = bl(tmp_path)
    n = 8

    # 不走 add/claim：那条路径一次只允许一个同名条目在 running 里，
    # 而这里要测的正是"多个同名条目同时归档"。直接在 running/ 摆好 n 个
    # 内容不同的文件，模拟 n 个 worker 各自跑完同一个任务名。
    running = q.dir(RUNNING)
    paths = []
    for i in range(n):
        p = running / f"same.{i}.staged.yaml"
        p.write_text(TASK_YAML + f"\n# worker {i}\n", encoding="utf-8")
        paths.append(p)

    errors: list[BaseException] = []
    barrier = threading.Barrier(n)   # 让 n 个线程尽量同时冲进 _park

    def worker(path):
        try:
            barrier.wait(timeout=5)
            # 都归档到同一个目标名 same.yaml —— 这才会触发后缀竞争
            q._park(path, NEEDS_HUMAN, name="same.yaml")
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发归档报错：{errors}"
    landed = list(q.dir(NEEDS_HUMAN).glob("same*.yaml"))
    assert len(landed) == n, f"{n} 个任务归档后只剩 {len(landed)} 份"
    bodies = {p.read_text(encoding="utf-8") for p in landed}
    assert len(bodies) == n, f"内容去重后只有 {len(bodies)} 种，有覆盖发生"


# ---------- M-1: 后缀有上限 ----------


def test_park_suffix_has_a_ceiling(tmp_path):
    """�register满上限要报错，不能无限自旋。"""
    q = bl(tmp_path)
    target_dir = q.dir(DONE)
    # 把所有候选名字都占掉
    (target_dir / "t.yaml").write_text("x", encoding="utf-8")
    for n in range(2, _PARK_MAX_SUFFIX + 1):
        (target_dir / f"t.{n}.yaml").write_text("x", encoding="utf-8")

    claim = q.claim(q.add(task_file(tmp_path / "src")))
    with pytest.raises(BacklogError, match=r"同名条目已有"):
        q.finish(claim, "merged")


# ---------- M-2: 体积上限 ----------


def test_add_rejects_oversized_file(tmp_path):
    """指错文件（日志、tarball）时报错，不要吃满内存。"""
    q = bl(tmp_path)
    big = tmp_path / "src" / "huge.yaml"
    big.parent.mkdir(parents=True, exist_ok=True)
    # 稀疏写：不真占磁盘，但 st_size 是真的
    with open(big, "wb") as fh:
        fh.truncate(MAX_TASK_BYTES + 1)

    with pytest.raises(BacklogError, match=r"任务文件太大"):
        q.add(big)
    assert list(q.dir(INBOX).iterdir()) == [], "被拒的文件不该留在 inbox"


def test_add_accepts_normal_sized_file(tmp_path):
    """上限不能把正常任务拦住。"""
    q = bl(tmp_path)
    dst = q.add(task_file(tmp_path / "src"))
    assert dst.read_text(encoding="utf-8") == TASK_YAML


# ---------- M-3: 原子写 ----------


def test_result_json_is_always_parseable(tmp_path):
    """.result.json 必须是完整 JSON。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src")))
    dst = q.finish(claim, "merged")

    result = dst.with_name(dst.name + ".result.json")
    assert result.is_file(), "没写 .result.json"
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["outcome"] == "merged"
    assert "wall_clock_s" in data, "字段在重构中丢了"


def test_atomic_write_leaves_no_tmp_on_success(tmp_path):
    """成功路径不留 .tmp 垃圾。"""
    target = tmp_path / "out.json"
    _atomic_write_text(target, '{"ok": true}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    strays = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert not strays, f"留下临时文件：{strays}"


def test_atomic_write_leaves_no_tmp_on_failure(tmp_path, monkeypatch):
    """写到一半炸了，也不留 .tmp，且原文件不被破坏。"""
    target = tmp_path / "out.json"
    target.write_text('{"original": true}', encoding="utf-8")

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("模拟 replace 失败")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _atomic_write_text(target, '{"new": true}')
    monkeypatch.setattr(os, "replace", real_replace)

    # 原文件必须完好 —— 这是原子写的全部意义
    assert json.loads(target.read_text(encoding="utf-8")) == {"original": True}
    strays = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert not strays, f"失败后留下临时文件：{strays}"


def test_atomic_write_survives_keyboard_interrupt(tmp_path, monkeypatch):
    """Ctrl-C 也要清理 —— except Exception 抓不住 KeyboardInterrupt。"""
    target = tmp_path / "out.json"

    def boom(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        _atomic_write_text(target, "{}")

    strays = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert not strays, f"中断后留下临时文件：{strays}"


def test_claim_file_is_atomic(tmp_path):
    """.claim 也走原子写，读到的必须是完整 JSON。"""
    q = bl(tmp_path)
    path = q.add(task_file(tmp_path / "src"))
    claim = q.claim(path)
    data = q.read_claim(claim.path)
    assert data["pid"] == os.getpid()
    assert "host" in data and "claimed_at" in data
