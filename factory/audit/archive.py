"""执行现场归档：把 transcript 和 diff 从易失位置搬进审计目录。

## 为什么需要这个模块

`sandbox_linux.prepare()` 给每次执行开一个 `mkdtemp(prefix="factory-transcript-")`
当 worker 的 `$HOME`，claude 把会话记录写在那棵树里。那个目录**刻意不注册进
ExitStack 清理**（见 sandbox_linux.py 的注释），所以跑完文件还在 —— 但它在
`/tmp` 下。

`/tmp` 的问题不是「会不会没」，是「什么时候没」：

  - systemd-tmpfiles 默认 10 天清一次（`/usr/lib/tmpfiles.d/tmp.conf`）
  - 机器重启，如果 /tmp 是 tmpfs，当场清零
  - 磁盘紧张时运维手动 `rm -rf /tmp/*`，谁都拦不住

实测（2026-08-26）宿主 `/tmp` 下堆了 1201 个 `factory-transcript-*` 目录，
`task_attempt.transcript_path` 一条条指着它们。**字段有值，文件随时会没。**
这是审计库在说谎：复盘时点开一个 attempt，路径写得清清楚楚，`open()` 报
FileNotFoundError。

diff 更彻底 —— 压根没存。`AttemptResult.diff` 带着完整 diff 正文回到
dispatcher，`record_result` 只取了 `diff_hash`，正文当场丢弃。前端想显示
「这一轮改了什么」，除了一个 64 位哈希什么都拿不到。

## 设计

一个函数 `archive_attempt()`，在 `record_result` 之前调用，把两样东西搬进
`<data_root>/attempts/<task_id>/<attempt_no>/`：

    transcript.jsonl   ← 从 /tmp 的出口目录**复制**过来
    diff.patch         ← AttemptResult.diff 的正文，直接写

复制而不是移动：出口目录里除了 transcript 还有 `.claude.json`（worker 的
一次性配置），移动会把整棵树搬过来，而那里面可能有中转站凭据。只挑
transcript 那一个文件。

返回归档后的路径，让调用方把**新路径**写进 `transcript_path` —— 老路径写进
库就等于把这个修复作废了。

## 失败策略：不抛

归档失败（盘满、权限不对、源文件已经没了）**不能让 attempt 失败**。这一轮
worker 可能已经把代码改对了，因为存不下日志就把整轮判死，是本末倒置。
所以每个操作都兜住异常，返回 None 表示「没归上」，让上层照原样落库。

代价是静默：归档一直失败也没人知道。所以返回值里带 `error`，dispatcher
把它并进 attempt 的 error_text —— 那条路径本来就会被人看到。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

#: 归档根目录名。挂在 data_root 下（audit.db 和 queue/ 的同级）。
#:
#: 不叫 transcripts/：这个目录装的不只是 transcript，还有 diff，以后可能
#: 有 worker 的 stdout。按 attempt 分目录而不是按类型分，是因为复盘的动作
#: 永远是「看某一轮的全部现场」，不是「看所有轮的 diff」。
ARCHIVE_DIRNAME = "attempts"

#: 归档里的固定文件名。前端按这两个名字取，不做 glob。
TRANSCRIPT_NAME = "transcript.jsonl"
DIFF_NAME = "diff.patch"


@dataclass(frozen=True)
class ArchiveResult:
    """一次归档的结果。两个路径各自可能为 None。"""

    #: 归档后的 transcript 绝对路径。None = 没归上（源不存在或复制失败）。
    transcript_path: str | None = None
    #: 归档后的 diff 绝对路径。None = 这一轮没有 diff，或写失败。
    diff_path: str | None = None
    #: 归档过程里的问题。空 = 一切正常。非空时调用方应该让它出现在
    #: 人能看到的地方（attempt 的 error_text / 日志），否则归档静默失效。
    error: str = ""


def attempt_dir(data_root: Path | str, task_id: str, attempt_no: int) -> Path:
    """`<data_root>/attempts/<safe_task_id>/<attempt_no>/`。

    task_id 要过一遍消毒：它来自任务 YAML，而 YAML 可能是网页投递来的。
    投递闸门限制了 `T-[a-z0-9-]+`，但归档模块不该依赖上游的校验还在 ——
    哪天闸门放宽（比如允许中文任务名），这里必须还是安全的。

    `..` 是唯一真正危险的形状（路径穿越）。`/` 会让 mkdir 建出嵌套目录，
    不算漏洞但会让归档散落，一并换掉。
    """
    safe = str(task_id).replace("/", "_").replace("\\", "_")
    safe = safe.replace("..", "_")
    # 空 task_id（理论上不该出现）会让归档直接落在 attempts/ 根下，
    # 和别的任务混在一起。给个显式的桶。
    safe = safe.strip() or "_unnamed"
    return Path(data_root) / ARCHIVE_DIRNAME / safe / str(attempt_no)


def archive_attempt(
    data_root: Path | str,
    task_id: str,
    attempt_no: int,
    *,
    transcript_src: str | Path | None,
    diff: str = "",
) -> ArchiveResult:
    """把一轮的执行现场搬进审计目录。**任何失败都不抛异常。**

    在 `record_result` 之前调用，用返回的 `transcript_path` 覆盖原来那个
    `/tmp` 路径再落库。

    `transcript_src` 为 None（非 claude harness、或 session_id 没拿到）时
    只归档 diff —— 不是错误，很多 harness 本来就没有 transcript。
    """
    problems: list[str] = []
    dest_dir = attempt_dir(data_root, task_id, attempt_no)

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 建不出目录，两样都别想归了。直接回，让上层照原样落库。
        return ArchiveResult(error=f"归档目录建不出（{dest_dir}）：{exc}")

    archived_transcript: str | None = None
    if transcript_src:
        src = Path(transcript_src)
        if not src.exists():
            # 源已经没了。这本身就是要报的事 —— 说明 /tmp 已经被清过一轮，
            # 而这次 attempt 的现场永久丢失了。
            problems.append(f"transcript 源文件不存在：{src}")
        else:
            dest = dest_dir / TRANSCRIPT_NAME
            try:
                # copy2 保留 mtime：复盘时「这份记录是什么时候写的」有用，
                # 而 copy 会把 mtime 改成归档时刻，把那个信息抹掉。
                shutil.copy2(src, dest)
                archived_transcript = str(dest)
            except OSError as exc:
                problems.append(f"transcript 复制失败（{src} → {dest}）：{exc}")

    archived_diff: str | None = None
    if diff:
        dest = dest_dir / DIFF_NAME
        try:
            # 显式 utf-8：diff 里有中文文件名或中文注释时，跟随 locale 的
            # 默认编码在 LANG=C 的 systemd 环境下会抛 UnicodeEncodeError。
            dest.write_text(diff, encoding="utf-8")
            archived_diff = str(dest)
        except OSError as exc:
            problems.append(f"diff 写入失败（{dest}）：{exc}")

    return ArchiveResult(
        transcript_path=archived_transcript,
        diff_path=archived_diff,
        error="；".join(problems),
    )


#: 单次读取的字节上限。transcript 实测 150KB 上下，但一个跑了 40 分钟、
#: 反复读大文件的 worker 能写出几十 MB —— 那种整份塞进 JSON 响应会把
#: 浏览器标签页打死。超限时截尾并在返回值里说明。
READ_LIMIT_BYTES = 2_000_000


def read_archived(path: str | Path | None, *, limit: int = READ_LIMIT_BYTES) -> tuple[str, str]:
    """读一份归档文件，返回 `(内容, 说明)`。

    说明非空时是给人看的一句话（文件没了 / 被截断了），前端应该显示它 ——
    静默返回空字符串会让人以为「这一轮没有日志」，而真相是「日志找不到了」。
    这两件事在复盘时的含义完全不同。

    不抛异常：这个函数在 HTTP 请求路径上，一个读文件的 OSError 变成 500
    对调用方毫无帮助。
    """
    if not path:
        return "", "没有记录这一轮的文件路径"
    p = Path(path)
    if not p.exists():
        return "", f"文件不存在：{p}（可能是归档前的老 attempt，现场已随 /tmp 清理丢失）"
    try:
        size = p.stat().st_size
        if size > limit:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(limit)
            return head, f"文件 {size:,} 字节，超过 {limit:,} 上限，只显示开头部分"
        return p.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as exc:
        return "", f"读取失败：{type(exc).__name__}: {exc}"
