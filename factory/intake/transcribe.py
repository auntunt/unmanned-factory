"""录音 → 文本。薄包装，不自己实现转录。

刻意做薄：转录本身有成熟工具（whisper / whisper.cpp / faster-whisper），
这里只负责调用、拿文本、失败时说清楚缺什么。把转录写进本仓库等于给
无人管道多一个自己维护的模型依赖。

失败必须**抛错而不是返回空串**。空串会一路流到抽取器，模型拿着空需求
照样能编出一个 task_id 和一段 prompt —— 然后派发出去的是凭空想象的任务。
入口层的空输入是最危险的一种，因为它不长得像错误。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded

#: 常见格式。不做转码 —— 交给 whisper（它内部走 ffmpeg）。
AUDIO_SUFFIXES = (".m4a", ".mp3", ".wav", ".mp4", ".mpga", ".webm", ".flac", ".ogg")

_DEFAULT_MODEL = "small"


class TranscribeError(RuntimeError):
    pass


def looks_like_audio(path: str | Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_SUFFIXES


def transcribe(
    audio: str | Path,
    *,
    binary: str = "whisper",
    model: str = _DEFAULT_MODEL,
    language: str | None = None,
    timeout_s: int = 900,
) -> str:
    """转录一个音频文件，返回纯文本。

    走 `--output_format txt --output_dir <tmp>` 而不是读 stdout：
    whisper 的 stdout 混着时间戳和进度条，解析它等于依赖它的界面格式。
    落到临时目录读 .txt 是它承诺的接口。
    """
    src = Path(audio)
    if not src.is_file():
        raise TranscribeError(f"音频文件不存在：{src}")
    if shutil.which(binary) is None:
        raise TranscribeError(
            f"找不到 {binary}。装一个：`pip install -U openai-whisper`"
            f"（需要 ffmpeg），或用 --text 直接给文字。"
        )

    with tempfile.TemporaryDirectory(prefix="factory-asr-") as out_dir:
        argv = [
            binary, str(src),
            "--model", model,
            "--output_format", "txt",
            "--output_dir", out_dir,
        ]
        if language:
            argv += ["--language", language]
        try:
            # 不是 subprocess.run：whisper 会拉起自己的 worker，超时只 kill
            # 顶上那个的话它们会接着占满 CPU。见 proc 模块。
            proc = run_bounded(argv, cwd=out_dir, timeout_s=timeout_s)
        except ProcTimeout:
            raise TranscribeError(f"转录超时（{timeout_s}s）：{src.name}")
        except OSError as exc:
            raise TranscribeError(f"无法启动 {binary}: {exc}")

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise TranscribeError(f"{binary} 退出码 {proc.returncode}：{tail}")

        hits = sorted(Path(out_dir).glob("*.txt"))
        if not hits:
            raise TranscribeError(
                f"{binary} 没有产出 txt。stderr 尾部："
                f"{(proc.stderr or '').strip()[-300:]}"
            )
        text = hits[0].read_text(encoding="utf-8", errors="replace").strip()

    if not text:
        # 空转录当错误。见模块注释：空输入不长得像错误，但后果最重。
        raise TranscribeError(
            f"转录结果为空：{src.name}。录音可能是静音或格式不支持。"
        )
    return text


def read_source(
    *,
    text: str | None = None,
    text_file: str | Path | None = None,
    audio: str | Path | None = None,
    **kw,
) -> str:
    """三种输入取其一，返回需求原文。三个都空就抛错。"""
    given = [k for k, v in
             (("--text", text), ("--text-file", text_file), ("--audio", audio))
             if v]
    if len(given) != 1:
        raise TranscribeError(
            f"需要且只能给一个输入源，实际给了 {given or '零个'}："
            "--text / --text-file / --audio"
        )
    if text:
        return text.strip()
    if text_file:
        p = Path(text_file)
        if not p.is_file():
            raise TranscribeError(f"文本文件不存在：{p}")
        body = p.read_text(encoding="utf-8", errors="replace").strip()
        if not body:
            raise TranscribeError(f"文本文件是空的：{p}")
        return body
    return transcribe(audio, **kw)
