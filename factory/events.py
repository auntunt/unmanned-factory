"""控制室事件流：live 和 replay 走同一种事件，前端不区分来源。

为什么单独一个模块而不是往 api.py 里加一个端点：

  1. 事件的形状是**前端契约**。live 从「两次快照的差」推出来，replay 从
     audit.db 的一条任务轨迹推出来，两条路必须产出一模一样的事件 —— 否则
     demo 时切 live 会露馅。把生成逻辑关在一处，契约才只有一份。
  2. 快照、求差、时间线三个都是纯函数，离线可测。SSE 那层（api.py）只负责
     把它们套进 while 循环加 sleep。

事件类型（`type` 字段）：

  snapshot        连上时先给一份全量：五个队列桶 + 所有 attempt 的当前态
  task.state      任务在队列里换了目录（inbox → running → done …）
  attempt.open    一轮开始：分级、模型
  attempt.line    agent 现场的一行（工具调用 / 一句话）
  attempt.result  一轮跑完：花费、token、墙钟、diff
  gate.verdict    一个监工出了判决
  attempt.final   一轮定案：merged / escalated / …
  heartbeat       没事发生时每几秒一条，让代理和浏览器知道连接还活着

每条都带 `at`（ISO，UTC）。replay 里 `at` 是**原始时间**，另有 `t`
（距任务首轮开始的秒数），前端按 `t` 排、按 `at` 显示 —— 回放里
「三个月前的任务」屏幕上也该显示三个月前的时刻，不然观众会问。

## live 的现场文本从哪来

transcript_path 在 attempt 跑完才落库（adapter 跑完才知道 session_id），
所以正在跑的那一轮没有路径可读。这里用 `~/.claude/projects/*/*.jsonl`
里 mtime 晚于该轮 created_at 的最新一个文件当现场 —— 是猜的，猜错的
后果只是屏幕上滚了别的会话的行，不进审计。并行多轮时按 mtime 顺序
逐个分给 created_at 顺序的 attempt，同样是猜。**这条路径只喂屏幕**。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from factory.audit.models import Resolution, TaskAttempt
from factory.audit.store import AuditStore
from factory.harness.transcript import projects_root

#: 屏幕上一行最长多少字符。超过就截，尾巴换成 …。demo 是隔着桌子看的，
#: 一行 200 字符谁也读不完，而且会把日志区撑得高低不齐。
LINE_MAX = 96

#: 一条 attempt.line 的 text 前缀，按工具名。没列的工具用原名。
_TOOL_VERB = {
    "Read": "读",
    "Edit": "改",
    "MultiEdit": "改",
    "Write": "写",
    "Bash": "跑",
    "Grep": "搜",
    "Glob": "找",
    "LS": "列",
    "WebFetch": "抓",
    "Task": "派子代理",
}


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) <= LINE_MAX:
        return text
    return text[: LINE_MAX - 1] + "…"


# ---------- transcript → 行 ----------


@dataclass(frozen=True)
class Line:
    """现场的一行。`ts` 是 transcript 里的时间戳（秒，可能没有）。"""

    text: str
    kind: str  # tool | text | error
    ts: float | None = None


def _parse_ts(raw: object) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tool_line(block: dict) -> str:
    name = str(block.get("name", "tool"))
    inp = block.get("input") or {}
    verb = _TOOL_VERB.get(name, name)
    if not isinstance(inp, dict):
        return verb
    target = (
        inp.get("file_path")
        or inp.get("path")
        or inp.get("command")
        or inp.get("pattern")
        or inp.get("url")
        or inp.get("description")
        or ""
    )
    return f"{verb} {target}".strip() if target else verb


def transcript_lines(path: str | Path | None, *, start: int = 0) -> tuple[Line, ...]:
    """把 Claude Code 的 session jsonl 变成屏幕上的行。

    只取三种东西：assistant 的 text（截短）、tool_use（人话化）、带 is_error
    的 tool_result（一条「✗」）。其余（system、进度、summary）全丢 ——
    屏幕要的是「它在干什么」，不是原始记录。

    `start` 是行号偏移：live 模式每次只读新出现的记录，避免每秒重解析
    整个 150KB 的文件。返回的行不含 start 之前的。
    """
    if not path:
        return ()
    p = Path(path)
    if not p.exists():
        return ()
    out: list[Line] = []
    try:
        raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ()
    for raw in raw_lines[start:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(rec.get("timestamp"))
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            if rec.get("type") == "assistant" and content.strip():
                out.append(Line(_clip(content), "text", ts))
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                out.append(Line(_clip(_tool_line(block)), "tool", ts))
            elif kind == "text" and rec.get("type") == "assistant":
                text = str(block.get("text", "")).strip()
                if text:
                    out.append(Line(_clip(text), "text", ts))
            elif kind == "tool_result" and block.get("is_error"):
                out.append(Line("✗ 上一步报错", "error", ts))
    return tuple(out)


def transcript_record_count(path: str | Path | None) -> int:
    """文件现在有多少条记录（给 live 的 `start` 用）。"""
    if not path:
        return 0
    p = Path(path)
    if not p.exists():
        return 0
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
    except OSError:
        return 0


def guess_live_transcripts(
    since: datetime, *, root: Path | None = None, limit: int = 8
) -> tuple[Path, ...]:
    """`since` 之后被写过的 session 文件，按 mtime 升序。模块 docstring 有说明。"""
    base = root or projects_root()
    if not base.exists():
        return ()
    # 留 2 秒余量：open_attempt 落库和 claude 建 session 文件几乎同时，
    # 两边的时钟精度不同，严格比较会把刚好同一秒的文件漏掉。
    cutoff = since.replace(tzinfo=UTC).timestamp() - 2.0
    found: list[tuple[float, Path]] = []
    try:
        for cand in base.glob("*/*.jsonl"):
            try:
                mt = cand.stat().st_mtime
            except OSError:
                continue
            if mt >= cutoff:
                found.append((mt, cand))
    except OSError:
        return ()
    found.sort()
    return tuple(p for _, p in found[-limit:])


# ---------- 快照 ----------


@dataclass(frozen=True)
class AttemptView:
    """一轮在屏幕上需要的全部字段。从 TaskAttempt 投影，不带 ORM 对象。"""

    id: int
    task_id: str
    attempt_no: int
    oracle_class: str
    class_reason: str
    model: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    wall_clock_ms: int
    diff_hash: str | None
    commit: str | None
    transcript_path: str | None
    resolution: str
    resolution_note: str
    created_at: datetime | None
    verdicts: tuple[tuple[str, str, int], ...]  # (role, verdict, claims)

    @property
    def started(self) -> bool:
        return True

    @property
    def has_result(self) -> bool:
        return self.wall_clock_ms > 0 or self.cost_usd > 0 or bool(self.diff_hash)

    @property
    def is_final(self) -> bool:
        return self.resolution != Resolution.PENDING.value

    @property
    def stage(self) -> str:
        """屏幕上「这一轮处在哪一步」。顺序：派发 → 判收 → 合并/定案。"""
        if self.is_final:
            return {
                Resolution.MERGED.value: "merged",
                Resolution.ESCALATED.value: "escalated",
            }.get(self.resolution, "closed")
        if self.verdicts:
            return "judging"
        if self.has_result:
            return "judging"
        return "dispatching"

    def public(self) -> dict:
        return {
            "task_id": self.task_id,
            "attempt_no": self.attempt_no,
            "oracle_class": self.oracle_class,
            "class_reason": self.class_reason,
            "model": self.model,
            "cost_usd": round(self.cost_usd, 4),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "wall_clock_ms": self.wall_clock_ms,
            "commit": self.commit,
            "resolution": self.resolution,
            "resolution_note": self.resolution_note,
            "stage": self.stage,
            "created_at": _iso(self.created_at),
            "verdicts": [
                {"role": r, "verdict": v, "claims": n} for r, v, n in self.verdicts
            ],
        }


def _view(a: TaskAttempt) -> AttemptView:
    cls = getattr(a, "oracle_class", "")
    res = getattr(a, "resolution", Resolution.PENDING)
    return AttemptView(
        id=a.id,
        task_id=a.task_id,
        attempt_no=a.attempt_no,
        oracle_class=str(getattr(cls, "value", cls) or ""),
        class_reason=a.class_reason or "",
        model=a.model or "",
        cost_usd=float(a.cost_usd or 0.0),
        tokens_in=int(a.tokens_in or 0),
        tokens_out=int(a.tokens_out or 0),
        wall_clock_ms=int(a.wall_clock_ms or 0),
        diff_hash=a.diff_hash,
        commit=a.commit,
        transcript_path=a.transcript_path,
        resolution=str(getattr(res, "value", res) or "pending"),
        resolution_note=a.resolution_note or "",
        created_at=a.created_at,
        verdicts=tuple(
            (
                str(getattr(v.role, "value", v.role)),
                str(getattr(v.verdict, "value", v.verdict)),
                len(v.claims or []),
            )
            for v in (a.supervisors or [])
        ),
    )


@dataclass
class Snapshot:
    """某一刻的全量状态。两份 Snapshot 相减得到事件。"""

    tasks: dict[str, str] = field(default_factory=dict)  # task_id → 桶名
    attempts: dict[tuple[str, int], AttemptView] = field(default_factory=dict)
    # 每个 attempt 的现场：文件路径 + 已经读到第几条
    tails: dict[tuple[str, int], tuple[str, int]] = field(default_factory=dict)
    at: str = ""

    def public(self) -> dict:
        buckets: dict[str, list[str]] = {}
        for tid, bucket in self.tasks.items():
            buckets.setdefault(bucket, []).append(tid)
        return {
            "type": "snapshot",
            "at": self.at or _now_iso(),
            "tasks": buckets,
            "attempts": [a.public() for a in sorted(
                self.attempts.values(), key=lambda a: (a.created_at or datetime.min, a.attempt_no)
            )],
        }


def take_snapshot(db: str | Path, queue: str | Path, *, prev: Snapshot | None = None) -> Snapshot:
    """读一次库和队列。`prev` 给了就沿用它的现场文件指针（避免重复猜路径）。"""
    from factory.api import list_tasks  # 函数内 import：api 反向依赖本模块

    snap = Snapshot(at=_now_iso())
    try:
        listed = list_tasks(queue)
    except Exception:  # noqa: BLE001 - 队列读不出来时屏幕上的桶为空，不炸流
        listed = {}
    for bucket, rows in listed.items():
        for row in rows:
            snap.tasks[str(row.get("task_id", ""))] = bucket

    for a in AuditStore(db).all_attempts():
        v = _view(a)
        snap.attempts[(v.task_id, v.attempt_no)] = v

    if prev is not None:
        for key, tail in prev.tails.items():
            if key in snap.attempts:
                snap.tails[key] = tail
    return snap


def diff_snapshots(prev: Snapshot | None, cur: Snapshot) -> list[dict]:
    """两份快照之间发生了什么。prev 为 None 时只产出一条 snapshot。

    顺序：任务状态 → 新开的轮 → 结果 → 判决 → 定案。同一轮的几件事在
    一秒内一起落库时也按这个顺序发，前端的状态机才不会先收到 final 再收到
    open。
    """
    if prev is None:
        return [cur.public()]
    out: list[dict] = []
    at = cur.at

    for tid, bucket in cur.tasks.items():
        if prev.tasks.get(tid) != bucket:
            out.append({"type": "task.state", "at": at, "task_id": tid, "state": bucket})

    for key, a in sorted(cur.attempts.items(), key=lambda kv: (kv[1].created_at or datetime.min, kv[0])):
        b = prev.attempts.get(key)
        base = {"at": at, "task_id": a.task_id, "attempt_no": a.attempt_no}
        if b is None:
            out.append({
                "type": "attempt.open", **base,
                "oracle_class": a.oracle_class, "class_reason": a.class_reason, "model": a.model,
            })
        if a.has_result and (b is None or not b.has_result):
            out.append({
                "type": "attempt.result", **base,
                "cost_usd": round(a.cost_usd, 4), "tokens_in": a.tokens_in,
                "tokens_out": a.tokens_out, "wall_clock_ms": a.wall_clock_ms,
                "has_diff": bool(a.diff_hash),
            })
        seen = set(b.verdicts) if b else set()
        for role, verdict, n in a.verdicts:
            if (role, verdict, n) not in seen:
                out.append({"type": "gate.verdict", **base, "role": role, "verdict": verdict, "claims": n})
        if a.is_final and (b is None or not b.is_final):
            out.append({
                "type": "attempt.final", **base,
                "resolution": a.resolution, "commit": a.commit, "note": a.resolution_note,
            })
    return out


def _running_keys(snap: Snapshot) -> list[tuple[str, int]]:
    """还没定案、还没结果的轮 —— 现场正在写的那些，按开始时间。"""
    keys = [k for k, a in snap.attempts.items() if not a.is_final and not a.has_result]
    keys.sort(key=lambda k: snap.attempts[k].created_at or datetime.min)
    return keys


def tail_lines(snap: Snapshot, *, root: Path | None = None) -> list[dict]:
    """给正在跑的轮各读一段新出现的现场行，并推进 snap.tails 指针。"""
    out: list[dict] = []
    running = _running_keys(snap)
    if not running:
        return out
    earliest = min(snap.attempts[k].created_at or datetime.now(UTC) for k in running)
    fresh = guess_live_transcripts(earliest, root=root)
    # 已经分配过路径的轮沿用；没分配的按顺序领一个还没被别的轮占用的文件
    taken = {p for p, _ in snap.tails.values()}
    for key in running:
        if key not in snap.tails:
            for cand in fresh:
                if str(cand) not in taken:
                    snap.tails[key] = (str(cand), 0)
                    taken.add(str(cand))
                    break
        if key not in snap.tails:
            continue
        path, start = snap.tails[key]
        lines = transcript_lines(path, start=start)
        total = transcript_record_count(path)
        snap.tails[key] = (path, total)
        a = snap.attempts[key]
        for ln in lines:
            out.append({
                "type": "attempt.line", "at": snap.at,
                "task_id": a.task_id, "attempt_no": a.attempt_no,
                "kind": ln.kind, "text": ln.text,
            })
    return out


# ---------- live ----------


def live_stream(
    db: str | Path,
    queue: str | Path,
    *,
    poll_s: float = 1.0,
    heartbeat_s: float = 10.0,
    stop: Callable[[], bool] = lambda: False,
    transcript_root: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict]:
    """一直产事件直到 stop() 为真。第一条是 snapshot。"""
    prev: Snapshot | None = None
    last_emit = time.monotonic()
    while not stop():
        cur = take_snapshot(db, queue, prev=prev)
        events = diff_snapshots(prev, cur)
        events.extend(tail_lines(cur, root=transcript_root))
        for ev in events:
            yield ev
            last_emit = time.monotonic()
        if time.monotonic() - last_emit >= heartbeat_s:
            yield {"type": "heartbeat", "at": _now_iso()}
            last_emit = time.monotonic()
        prev = cur
        sleep(poll_s)


# ---------- replay ----------


def replay_timeline(db: str | Path, task_id: str, *, transcript_root: Path | None = None) -> list[dict]:
    """把一个任务的审计轨迹展开成带 `t` 的事件序列。

    时间从哪来：attempt.created_at 是开始；现场行用 transcript 自带的时间戳
    （没有就在这一轮的墙钟里均匀铺开）；result 落在 created_at + wall_clock；
    判决和定案没有自己的时间戳 —— 审计库不记（它们几秒内连着写），这里
    把它们排在 result 之后、每个隔 1.5 秒，让闸门一盏盏亮而不是一起亮。
    这 1.5 秒是**编的**，只影响回放节奏；`at` 字段仍是真实的落库时刻。
    """
    store = AuditStore(db)
    attempts = [_view(a) for a in store.attempts_for(task_id)]
    if not attempts:
        return []
    t0 = min((a.created_at for a in attempts if a.created_at), default=None)
    if t0 is None:
        t0 = datetime.now(UTC).replace(tzinfo=None)
    origin = t0.replace(tzinfo=UTC).timestamp()

    def rel(dt: datetime | None, extra_s: float = 0.0) -> float:
        base = (dt or t0).replace(tzinfo=UTC).timestamp()
        return max(0.0, base - origin + extra_s)

    events: list[dict] = []
    first = attempts[0]
    events.append({
        "type": "task.state", "t": 0.0, "at": _iso(first.created_at),
        "task_id": task_id, "state": "running",
    })
    last_t = 0.0
    for a in attempts:
        start = rel(a.created_at)
        base = {"task_id": task_id, "attempt_no": a.attempt_no}
        events.append({
            "type": "attempt.open", "t": start, "at": _iso(a.created_at), **base,
            "oracle_class": a.oracle_class, "class_reason": a.class_reason, "model": a.model,
        })
        wall = max(a.wall_clock_ms / 1000.0, 1.0)
        end = start + wall

        path = a.transcript_path
        if path and transcript_root is not None and not Path(path).is_absolute():
            path = str(transcript_root / path)
        lines = transcript_lines(path)
        n = len(lines)
        for i, ln in enumerate(lines):
            if ln.ts is not None and ln.ts >= origin:
                tl = ln.ts - origin
                if tl > end:
                    tl = end
            else:
                tl = start + wall * (i + 1) / (n + 1)
            events.append({
                "type": "attempt.line", "t": tl, "at": _iso(a.created_at), **base,
                "kind": ln.kind, "text": ln.text,
            })

        if a.has_result:
            events.append({
                "type": "attempt.result", "t": end, "at": _iso(a.created_at), **base,
                "cost_usd": round(a.cost_usd, 4), "tokens_in": a.tokens_in,
                "tokens_out": a.tokens_out, "wall_clock_ms": a.wall_clock_ms,
                "has_diff": bool(a.diff_hash),
            })
        cursor = end
        for role, verdict, cnt in a.verdicts:
            cursor += 1.5
            events.append({
                "type": "gate.verdict", "t": cursor, "at": _iso(a.created_at), **base,
                "role": role, "verdict": verdict, "claims": cnt,
            })
        if a.is_final:
            cursor += 1.5
            events.append({
                "type": "attempt.final", "t": cursor, "at": _iso(a.created_at), **base,
                "resolution": a.resolution, "commit": a.commit, "note": a.resolution_note,
            })
        last_t = max(last_t, cursor)

    final = attempts[-1]
    bucket = {
        Resolution.MERGED.value: "done",
        Resolution.ESCALATED.value: "needs_human",
    }.get(final.resolution)
    if bucket:
        events.append({
            "type": "task.state", "t": last_t + 0.5, "at": _iso(final.created_at),
            "task_id": task_id, "state": bucket,
        })
    events.sort(key=lambda e: e["t"])
    return events


def replay_stream(
    db: str | Path,
    task_id: str,
    *,
    speed: float = 1.0,
    stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    transcript_root: Path | None = None,
) -> Iterator[dict]:
    """按 `t` 的间隔（除以 speed）逐条产出。第一条是只含这一个任务的 snapshot。

    speed ≤ 0 视为「全部立刻给」—— 前端要一次拿到整条时间线自己控制节奏时用。
    """
    timeline = replay_timeline(db, task_id, transcript_root=transcript_root)
    snap = Snapshot(at=_now_iso(), tasks={task_id: "inbox"})
    yield {**snap.public(), "replay": task_id, "total": len(timeline)}
    clock = 0.0
    for ev in timeline:
        if stop():
            return
        gap = ev["t"] - clock
        if speed > 0 and gap > 0:
            sleep(gap / speed)
        clock = ev["t"]
        yield ev
    yield {"type": "replay.end", "at": _now_iso(), "task_id": task_id}
