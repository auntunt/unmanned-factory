"""transcribe() 测试。用假 whisper 脚本，不装真模型。

这个文件是补的：转录这条路之前一条测试都没有 —— 它是唯一一条**人**在环里
的入口（口述需求），失败的时候人就在旁边，所以一直没被当回事。但它同样有
超时，同样会漏进程树，而 whisper 会按 CPU 核数拉起 worker。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.intake.transcribe import TranscribeError, transcribe


def _fake_whisper(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "whisper"
    script.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    script.chmod(0o755)
    return script


def _audio(tmp_path: Path) -> Path:
    src = tmp_path / "note.m4a"
    src.write_bytes(b"not really audio")
    return src


def test_missing_audio_file_is_named_in_the_error(tmp_path):
    with pytest.raises(TranscribeError, match="不存在"):
        transcribe(tmp_path / "nope.m4a")


def test_missing_binary_says_how_to_install_it(tmp_path):
    with pytest.raises(TranscribeError, match="找不到"):
        transcribe(_audio(tmp_path), binary="whisper-that-does-not-exist")


def test_output_comes_from_the_txt_file_not_stdout(tmp_path):
    """stdout 混着进度条和时间戳；.txt 才是 whisper 承诺的接口。"""
    fake = _fake_whisper(tmp_path, '''
out=""
while [ $# -gt 0 ]; do
  [ "$1" = "--output_dir" ] && out="$2"
  shift
done
echo "[00:00.000 --> 00:02.000] 进度条噪音"
printf '把登录改成手机号\\n' > "$out/note.txt"
''')
    text = transcribe(_audio(tmp_path), binary=str(fake))
    assert text == "把登录改成手机号"
    assert "进度条噪音" not in text


def test_nonzero_exit_surfaces_stderr_tail(tmp_path):
    fake = _fake_whisper(tmp_path, 'echo "ffmpeg not found" >&2\nexit 4\n')
    with pytest.raises(TranscribeError, match="退出码 4"):
        transcribe(_audio(tmp_path), binary=str(fake))


def test_exit_zero_without_a_txt_is_still_an_error(tmp_path):
    """退出 0 但没产出文件 —— 静默返回空字符串会让需求凭空变成空的。"""
    fake = _fake_whisper(tmp_path, "exit 0\n")
    with pytest.raises(TranscribeError, match="没有产出 txt"):
        transcribe(_audio(tmp_path), binary=str(fake))


def test_a_timed_out_transcription_leaves_no_children_behind(
    tmp_path, leak_probe
):
    """whisper 按核数拉 worker，超时只 kill 顶上那个会留一屋子满载进程。"""
    probe = leak_probe("whisper")
    with pytest.raises(TranscribeError, match="转录超时"):
        transcribe(_audio(tmp_path), binary=str(probe.script), timeout_s=1)
    probe.assert_reaped()
