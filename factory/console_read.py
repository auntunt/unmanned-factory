"""把三处数据源读成 Thread 列表。console.py 的数据层。

拆成单独模块是因为渲染和读取的失败模式不一样：读不到队列要显示「读不到」而
不是「空的」（dashboard 那边踩过这个坑，见 queue_state 的 .error），
而渲染失败只是难看。混在一个文件里，读取的 try 边界会跟着 HTML 拼接一起长。
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from factory.backlog.journal import Journal
from factory.backlog.store import (
    BLOCKED,
    DONE,
    INBOX,
    LOG,
    NEEDS_HUMAN,
    RUNNING,
    Backlog,
)
from factory.console import OK, STOP, WAIT, Step, Thread

#: 队列状态 → (阶段, 成色, 说法)。
#:
#: needs-human 和 blocked 都是 STOP 但话不一样：前者是「跑过没过验收，去看
#: diff」，后者是「这类改动永不许无人跑」。混成一句话，用户不知道该干什么。
_STATE_STEP: dict[str, tuple[str, str, str]] = {
    INBOX: ("queued", WAIT, "已入队，等空闲执行位"),
    RUNNING: ("running", WAIT, "worker 正在改代码并自测"),
    DONE: ("landed", OK, "验收通过，改动已落在分支上"),
    NEEDS_HUMAN: ("review", STOP, "没通过验收，需要你看一眼 diff"),
    BLOCKED: ("gate", STOP, "涉及不可逆操作，按规矩不允许无人执行"),
}

_YAML_SUFFIX = (".yaml", ".yml")


def _title_of(path: Path) -> tuple[str, str]:
    """从 Task YAML 里取标题和原始需求。

    prompt 的第一行当标题 —— YAML 里没有 title 字段，而 task_id 是 slug
    （`add-forgot-password`），给人看不如原话。
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return path.stem, ""
    prompt = str(doc.get("prompt") or "").strip()
    if not prompt:
        return path.stem, ""
    first = prompt.splitlines()[0].strip()
    return (first[:80] or path.stem), prompt


def _scan_queue(root: str | Path) -> dict[str, dict]:
    """扫队列目录，每个 task 一条记录。

    直接走文件系统而不复用 dashboard.queue_state：那边返回的 QueueEntry 不带
    mtime 和 prompt，而这里两个都要（时间轴要排序，卡片要显示原话）。
    """
    # 先判「这地方到底是不是一个队列」。不判的话下面每个 is_dir() 都返 False，
    # 于是路径写错、目录被删、指到一个文件上，全都渲染成「还没有任务」——
    # 而那句话的意思是「你没提过需求」，用户会接着提，然后发现还是什么都没有。
    # dashboard.queue_state 里有同一条注释，同一个坑。
    p = Path(root)
    if not p.is_dir():
        raise FileNotFoundError(f"{p} 不是一个目录")
    if not any((p / s).is_dir() for s in (INBOX, RUNNING, DONE, NEEDS_HUMAN)):
        raise FileNotFoundError(
            f"{p} 下面没有 inbox/running/done/needs-human，不像一个队列目录")

    bl = Backlog(root)
    out: dict[str, dict] = {}
    for state in (INBOX, RUNNING, DONE, NEEDS_HUMAN, BLOCKED):
        d = bl.dir(state)
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix not in _YAML_SUFFIX:
                continue
            title, said = _title_of(p)
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = time.time()
            out[p.stem] = {"state": state, "title": title, "said": said,
                           "mtime": mtime, "path": str(p)}
    return out


def _journal_events(root: str | Path, *, limit: int = 400) -> list[dict]:
    """读队列日志。读不到就返回空 —— 日志缺失不该让页面挂掉。"""
    try:
        return list(Journal(Backlog(root).dir(LOG)).tail(limit=limit))
    except Exception:
        return []


def _gate_step(ev: dict) -> Step:
    """gate 事件 → 一条人话。

    codes 是 `[dangling-spec-ref]` 这种内部码，直接显示等于让用户去读源码。
    翻译不了的照原样带出来 —— 显示一个陌生的码，比丢掉这条信息好。
    """
    codes = [str(c) for c in (ev.get("codes") or [])]
    if ev.get("admitted"):
        return Step("gate", OK, "闸门通过，判据可执行", ts=ev.get("ts"),
                    detail=("提示：" + "、".join(codes)) if codes else "")
    hint = "、".join(_CODE_TEXT.get(c, c) for c in codes) or "判据不完整"
    return Step("gate", STOP, f"闸门拦下：{hint}", ts=ev.get("ts"),
                detail="补齐后可以重提，或者在下面直接改这条任务")


#: 闸门码 → 人话。缺的会原样显示，所以不用穷举。
_CODE_TEXT = {
    "dangling-spec-ref": "引用了规格编号但没给文档路径",
    "no-checks": "没有可自动执行的验收判据",
    "empty-acceptance": "没写验收标准",
    "dangerous-op": "涉及危险操作",
    "blocked_hard_gate": "属于不可逆操作，不允许无人执行",
}


def _dispatch_step(ev: dict) -> Step:
    """dispatch 事件 → 一条人话。outcome 决定成色。"""
    outcome = str(ev.get("outcome") or "")
    cost = float(ev.get("cost_usd") or 0.0)
    secs = float(ev.get("wall_clock_s") or 0.0)
    money = f"花了 ${cost:.4f}" if cost else "未计价"
    took = f"用了 {secs / 60:.1f} 分钟" if secs >= 60 else f"用了 {secs:.0f} 秒"
    note = str(ev.get("note") or "").strip()

    if outcome == "merged":
        return Step("landed", OK, f"验收通过，{took}，{money}", ts=ev.get("ts"),
                    detail=note)
    if outcome in ("escalated", "needs_human"):
        return Step("review", STOP, f"三轮没过验收，升级给人（{took}，{money}）",
                    ts=ev.get("ts"), detail=note or "去看 diff 决定是否手动收")
    if outcome == "blocked_hard_gate":
        return Step("gate", STOP, "硬闸门：只生成脚本，不代为执行",
                    ts=ev.get("ts"), detail=note)
    if outcome == "error":
        return Step("running", STOP, f"派发出错（{money}）", ts=ev.get("ts"),
                    detail=note or "看队列日志里的原因")
    return Step("review", WAIT, f"{outcome or '处理中'}（{took}，{money}）",
                ts=ev.get("ts"), detail=note)


def _recover_step(ev: dict) -> Step:
    return Step("running", STOP, "上一轮跑到一半崩了，已捞回来",
                ts=ev.get("ts"),
                detail=str(ev.get("reason") or "") or "会在下一轮重试")


def _deadlock_step(ev: dict) -> Step:
    waiting = "、".join(str(x) for x in (ev.get("missing") or []))
    return Step("queued", STOP, "永远等不到前置任务",
                ts=ev.get("ts"),
                detail=f"在等：{waiting}" if waiting else "前置任务不存在")


_EVENT_STEP = {
    "gate": _gate_step,
    "dispatch": _dispatch_step,
    "recover": _recover_step,
    "dep_deadlock": _deadlock_step,
}


def read_threads(queue_root: str | Path | None, *, limit: int = 40
                 ) -> tuple[list[Thread], str]:
    """读出所有需求的经过。返回 (threads, error)。

    error 非空时 threads 可能是部分结果 —— 调用方必须把 error 显示出来。
    「读不到队列」和「队列是空的」在页面上长得一样，而前者意味着页面在说谎。
    这一课是 dashboard 的 queue_state 用注释记下来的，这里照办。
    """
    if queue_root is None:
        return [], "没有配置队列目录（--queue），无法显示任务进度"

    try:
        entries = _scan_queue(queue_root)
    except Exception as exc:  # 目录不存在、权限、指到文件上
        return [], f"读队列失败：{exc}"

    events = _journal_events(queue_root)

    # 事件按 task_id 归组。task_id 可能带 `.2` 撞名后缀，而 journal 里记的是
    # 写入时的 draft.task_id（不带后缀）—— 用 stem 前缀兜一层，否则重名任务的
    # 日志会全部挂到第一个上。
    by_task: dict[str, list[dict]] = {}
    for ev in events:
        tid = str(ev.get("task_id") or "")
        if tid:
            by_task.setdefault(tid, []).append(ev)

    threads: list[Thread] = []
    for stem, meta in entries.items():
        base = stem.split(".")[0]
        evs = by_task.get(stem) or by_task.get(base) or []

        steps: list[Step] = []
        first_ts = min((float(e.get("ts") or 0) for e in evs), default=0.0) \
            or meta["mtime"]
        steps.append(Step("intake", OK, "需求已转成任务书", ts=first_ts))

        for ev in sorted(evs, key=lambda e: float(e.get("ts") or 0)):
            fn = _EVENT_STEP.get(str(ev.get("kind")))
            if fn:
                steps.append(fn(ev))

        # 队列当前位置是**权威**：journal 可能缺事件（日志写失败不抛），
        # 但文件在哪个目录里是事实。用它兜最后一条，避免「日志说在跑、
        # 其实早就 merged 了」这种页面撒谎。
        stage, tone, text = _STATE_STEP[meta["state"]]
        if not steps or steps[-1].stage != stage or steps[-1].tone != tone:
            steps.append(Step(stage, tone, text, ts=meta["mtime"]))

        threads.append(Thread(
            thread_id=stem, title=meta["title"], created_at=first_ts,
            steps=steps, task_ids=[stem], said=meta["said"],
        ))

    threads.sort(key=lambda t: t.created_at, reverse=True)
    return threads[:limit], ""
