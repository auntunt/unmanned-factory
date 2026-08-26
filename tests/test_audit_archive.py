"""归档模块：执行现场从 /tmp 搬进审计目录。

这些测试盯的是一件事 —— **字段有值但文件没了** 这个具体故障。所以重点不在
「正常路径能跑通」，而在每一种归档失败下 attempt 是否照样活着。
"""

from __future__ import annotations

from pathlib import Path

from factory.audit.archive import (
    ARCHIVE_DIRNAME,
    DIFF_NAME,
    TRANSCRIPT_NAME,
    archive_attempt,
    attempt_dir,
    read_archived,
)


def _fake_transcript(tmp_path: Path, text: str = '{"type":"user"}\n') -> Path:
    """造一份 claude 风格的 transcript，放在模拟的 /tmp 出口目录里。"""
    src = tmp_path / "factory-transcript-abc123" / ".claude" / "projects" / "-slug"
    src.mkdir(parents=True)
    f = src / "session-id.jsonl"
    f.write_text(text, encoding="utf-8")
    return f


def test_归档把两样都落到审计目录(tmp_path: Path) -> None:
    src = _fake_transcript(tmp_path, '{"a":1}\n{"b":2}\n')
    root = tmp_path / "factory-data"

    got = archive_attempt(
        root, "T-demo", 3, transcript_src=src, diff="--- a/x\n+++ b/x\n+hi\n"
    )

    assert got.error == ""
    assert got.transcript_path is not None
    assert got.diff_path is not None
    # 路径落在 <root>/attempts/<task>/<no>/ 下，而不是原来的 /tmp
    dest = root / ARCHIVE_DIRNAME / "T-demo" / "3"
    assert Path(got.transcript_path) == dest / TRANSCRIPT_NAME
    assert Path(got.diff_path) == dest / DIFF_NAME
    # 内容一字不差
    assert Path(got.transcript_path).read_text() == '{"a":1}\n{"b":2}\n'
    assert Path(got.diff_path).read_text() == "--- a/x\n+++ b/x\n+hi\n"


def test_源文件已被清掉时报告问题但不抛(tmp_path: Path) -> None:
    """/tmp 被清过一轮之后的样子。这是本次修复要防的原始故障。"""
    got = archive_attempt(
        tmp_path, "T-demo", 1, transcript_src="/tmp/factory-transcript-gone/x.jsonl"
    )

    assert got.transcript_path is None
    # 必须留话 —— 静默返回 None 会让人以为「这轮本来就没日志」
    assert "不存在" in got.error


def test_没有transcript的harness只归diff(tmp_path: Path) -> None:
    """shell harness 之类根本没有 transcript，这不是错误。"""
    got = archive_attempt(tmp_path, "T-demo", 1, transcript_src=None, diff="patch body")

    assert got.transcript_path is None
    assert got.diff_path is not None
    assert got.error == ""  # 没有 transcript 不该报错


def test_空diff不建文件(tmp_path: Path) -> None:
    """worker 什么都没改的轮次。空 diff.patch 会让人误以为文件损坏。"""
    src = _fake_transcript(tmp_path)
    got = archive_attempt(tmp_path / "d", "T-demo", 1, transcript_src=src, diff="")

    assert got.diff_path is None
    assert not (attempt_dir(tmp_path / "d", "T-demo", 1) / DIFF_NAME).exists()


def test_归档目录建不出来时不抛(tmp_path: Path) -> None:
    """把 data_root 指向一个普通文件 —— mkdir 必然 OSError。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")

    got = archive_attempt(blocker, "T-demo", 1, transcript_src=None, diff="body")

    assert got.transcript_path is None and got.diff_path is None
    assert "归档目录建不出" in got.error


def test_task_id里的路径穿越被挡住(tmp_path: Path) -> None:
    """task_id 来自 YAML，可能是网页投递的。`..` 不能穿出 attempts/。"""
    root = tmp_path / "data"
    got = archive_attempt(root, "../../etc/evil", 1, transcript_src=None, diff="x")

    assert got.diff_path is not None
    # 归档结果必须仍在 attempts/ 树内
    assert (root / ARCHIVE_DIRNAME).resolve() in Path(got.diff_path).resolve().parents


def test_中文diff用utf8写(tmp_path: Path) -> None:
    """LANG=C 的 systemd 环境下跟随 locale 编码会抛 UnicodeEncodeError。"""
    got = archive_attempt(
        tmp_path, "T-demo", 1, transcript_src=None, diff="+# 中文注释\n"
    )

    assert got.error == ""
    assert Path(got.diff_path or "").read_text(encoding="utf-8") == "+# 中文注释\n"


# ---------- read_archived：给 HTTP 请求路径用，任何输入都不许抛 ----------


def test_读归档文件返回内容且无说明(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    f.write_text("line1\nline2\n", encoding="utf-8")

    body, note = read_archived(f)

    assert body == "line1\nline2\n"
    assert note == ""  # 正常读到就不该有话说


def test_路径为空时说清楚是没记路径(tmp_path: Path) -> None:
    body, note = read_archived(None)

    assert body == ""
    assert "没有记录" in note


def test_文件不存在时区分于内容为空(tmp_path: Path) -> None:
    """老 attempt 的现场随 /tmp 丢了。这跟「这轮没日志」必须能分辨。"""
    body, note = read_archived(tmp_path / "gone.jsonl")

    assert body == ""
    assert "不存在" in note and "/tmp" in note


def test_超大文件截尾并说明(tmp_path: Path) -> None:
    f = tmp_path / "big.jsonl"
    f.write_text("x" * 5000, encoding="utf-8")

    body, note = read_archived(f, limit=1000)

    assert len(body) == 1000
    assert "超过" in note and "5,000" in note


def test_二进制垃圾不抛异常(tmp_path: Path) -> None:
    """transcript 理论上是 utf-8，但盘坏了/写一半断电会留下非法字节。"""
    f = tmp_path / "broken.jsonl"
    f.write_bytes(b'{"a":1}\n\xff\xfe garbage\n')

    body, note = read_archived(f)

    assert '{"a":1}' in body  # errors="replace"，能读多少算多少
    assert note == ""
