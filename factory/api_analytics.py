"""`GET /api/analytics` —— 一个请求喂完整个总览页。

## 为什么是一个端点而不是八个

总览页要画的东西：闭环环形图、趋势、成本、判据表、人工验收缺口、滞留任务。
拆成八个端点的后果实测过一次（OA 那套监控台把 tickets 全量 302KB 每 10 秒
轮一遍）：页面首屏发 N 个并发请求，其中任何一个慢就整页参差不齐地跳；而每个
端点各自 `all_attempts()` 扫一遍库，同一份数据被读 N 遍。

这里反过来：**一次 `all_attempts()`，在内存里分八路聚合**。当前库 26 轮，
聚合完的 JSON 约 4KB。库涨到十万轮时这个函数要改成 SQL 聚合，但那时改的是
这一个函数，前端契约不动。

## 口径必须写进字段名

同一个「自动化率」在不同分母下能差一倍（OA 那套系统里看板 63% / 总览 83.1%，
用户当场质疑数据不一致）。所以这里所有比率都带显式分母：`closed_rate` 旁边
一定有 `closed_of`、`total_of`。前端不做任何除法 —— 它拿到的每个百分比都
已经和它的分母绑在一起了。

## null 不是 0

拿不到的数给 `None`，前端渲染 `—`。队列读不到时给五个 0，页面会显示「队列是
空的」，那是编造。这条和 `global_stats` 的处理保持一致。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from factory.audit.store import AuditStore

#: 闭环环形图的节点顺序。key 与 `stage_counts` 的键一一对应。
#:
#: `source` 区分真实态和派生态，抄的是 OA 监控台复刻里最值钱的一条经验：
#: 那套系统后端只有 4 个状态，前端画 8 个节点，其中 4 个是从字段推导的
#: 瞬时态、实测计数恒为 0。不标注来源的话，运维看见「复核中 0」会以为
#: 复核环节没跑，实际是任务在那儿停留不到一秒。
#:
#:   real    —— 队列目录里数得出来的，是当下事实
#:   derived —— 从 attempt 字段推导的瞬时态，计数低不代表环节没工作
STAGE_NODES: tuple[dict, str] = (
    {"key": "inbox", "name": "待派发", "source": "real",
     "desc": "已投递，等 dispatcher 认领。"},
    {"key": "running", "name": "执行中", "source": "real",
     "desc": "worker 正在跑。停留时间通常是分钟级。"},
    {"key": "judging", "name": "判据裁决", "source": "derived",
     "derived_from": "最新一轮有 verdicts 但还未定案",
     "desc": "监工正在核对判据。瞬时态，计数低不代表没跑。"},
    {"key": "reworking", "name": "返工中", "source": "derived",
     "derived_from": "resolution=reworked 且该任务仍在队列里",
     "desc": "判据不过，打回重做。这里的累计次数是成本大头。"},
    {"key": "needs_human", "name": "待人介入", "source": "real",
     "desc": "机器搞不定，等人定案。滞留在这里的时长是真实卡点。"},
    {"key": "blocked", "name": "已阻塞", "source": "real",
     "desc": "投递闸门或依赖问题，未进入执行。"},
    {"key": "done", "name": "已完成", "source": "real", "terminal": True,
     "desc": "队列条目已归档。注意「完成」不等于「验收通过」。"},
)

#: resolution → 是否算「机器自己搞定了」。
#: 只有 merged 算。reworked 是中间态，escalated/blocked 都要人。
_AUTO_RESOLVED = {"merged"}


def _iso_day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _core(attempts: tuple, task_ids: set[str], window_days: int) -> dict:
    """五个头部 KPI。每个都带 `of`（分母）和 `basis`（口径文字）。"""
    now = datetime.now()
    cutoff = now - timedelta(days=window_days)

    # 按任务归并 resolution：一个任务多轮，只有最后一轮的 resolution 算数。
    last_by_task: dict[str, object] = {}
    for a in attempts:
        last_by_task[a.task_id] = a

    total_tasks = len(task_ids)
    auto_done = sum(1 for a in last_by_task.values()
                    if a.resolution in _AUTO_RESOLVED)

    # 近 N 天：按 attempt 的 created_at 落窗，不是按任务
    recent = [a for a in attempts if a.created_at >= cutoff]
    recent_tasks = {a.task_id for a in recent}
    recent_auto = sum(
        1 for tid in recent_tasks
        if tid in last_by_task and last_by_task[tid].resolution in _AUTO_RESOLVED
    )

    human_passed = sum(1 for a in attempts if a.human_verdict == "pass")
    human_failed = sum(1 for a in attempts if a.human_verdict == "fail")
    human_judged = human_passed + human_failed

    return {
        "auto_resolved_rate": {
            "value": round(auto_done / total_tasks, 4) if total_tasks else None,
            "of": total_tasks,
            "hit": auto_done,
            "basis": "全量任务 · 末轮 resolution=merged",
            "label": "机器自闭环率",
        },
        "recent_auto_rate": {
            "value": (round(recent_auto / len(recent_tasks), 4)
                      if recent_tasks else None),
            "of": len(recent_tasks),
            "hit": recent_auto,
            "basis": f"近 {window_days} 天有执行的任务",
            "label": f"近 {window_days} 天自闭环率",
        },
        "human_pass_rate": {
            # 分母刻意排除 pending：没人验过的轮次算不通过会让通过率虚低，
            # 而「还没验」本身要单独显示成缺口（下面的 coverage）。
            "value": round(human_passed / human_judged, 4) if human_judged else None,
            "of": human_judged,
            "hit": human_passed,
            "basis": "已验收轮次（不含 pending）",
            "label": "人工验收通过率",
        },
        "human_coverage": {
            "value": (round(human_judged / len(attempts), 4)
                      if attempts else None),
            "of": len(attempts),
            "hit": human_judged,
            "basis": "全量轮次中已被人验过的比例",
            "label": "验收覆盖率",
        },
        "rework_depth": {
            # 不是比率，是平均轮次。value 直接给数，前端不加 %。
            "value": (round(len(attempts) / total_tasks, 2)
                      if total_tasks else None),
            "of": total_tasks,
            "hit": len(attempts),
            "basis": "总轮次 / 任务数",
            "label": "平均轮次/任务",
            "unit": "轮",
        },
    }


def _stage_counts(queue_counts: dict, attempts: tuple, in_queue: dict) -> list[dict]:
    """闭环环形图的七个节点计数。

    真实态直接取队列目录的计数；派生态从 attempt 推。派生态计数低是正常的
    （任务在那儿停留极短），所以每个节点都带 `source`，前端必须把这件事
    显示出来 —— 否则「判据裁决 0」会被读成「监工没跑」。
    """
    derived_judging = 0
    derived_rework = 0
    latest: dict[str, object] = {}
    for a in attempts:
        latest[a.task_id] = a
    for tid, a in latest.items():
        # 只算还在队列里的任务：已归档任务的末轮状态不是「当下」。
        if tid not in in_queue:
            continue
        if a.resolution == "reworked":
            derived_rework += 1
        elif a.supervisors and a.resolution == "pending":
            derived_judging += 1

    out = []
    for node in STAGE_NODES:
        key = node["key"]
        if node["source"] == "real":
            count = queue_counts.get(key)          # 可能是 None（队列读不到）
        elif key == "judging":
            count = derived_judging
        elif key == "reworking":
            count = derived_rework
        else:
            count = 0
        out.append({**node, "count": count})
    return out


def _trend(attempts: tuple, days: int) -> list[dict]:
    """按天的轮次 / 花费 / 定案。缺的日期补 0（那天确实没跑）。"""
    now = datetime.now()
    start = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    runs: Counter = Counter()
    cost: defaultdict = defaultdict(float)
    merged: Counter = Counter()
    for a in attempts:
        if a.created_at < start:
            continue
        d = _iso_day(a.created_at)
        runs[d] += 1
        cost[d] += a.cost_usd
        if a.resolution == "merged":
            merged[d] += 1

    return [
        {
            "date": (day := _iso_day(start + timedelta(days=i))),
            "runs": runs.get(day, 0),
            "merged": merged.get(day, 0),
            "cost_usd": round(cost.get(day, 0.0), 4),
        }
        for i in range(days)
    ]


def _cost(attempts: tuple, task_ids: set[str]) -> dict:
    """成本构成。按模型拆，因为换模型是最直接的降本手段。"""
    by_model: defaultdict = defaultdict(
        lambda: {"runs": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    )
    for a in attempts:
        b = by_model[a.model or "(未记录)"]
        b["runs"] += 1
        b["cost_usd"] += a.cost_usd
        b["tokens_in"] += a.tokens_in
        b["tokens_out"] += a.tokens_out

    total = sum(a.cost_usd for a in attempts)
    models = sorted(
        (
            {"model": m, **{k: (round(v, 4) if isinstance(v, float) else v)
                            for k, v in vals.items()},
             "share": round(vals["cost_usd"] / total, 4) if total else None}
            for m, vals in by_model.items()
        ),
        key=lambda r: -r["cost_usd"],
    )

    return {
        "total_usd": round(total, 6),
        "per_task_usd": round(total / len(task_ids), 6) if task_ids else None,
        "tokens_in": sum(a.tokens_in for a in attempts),
        "tokens_out": sum(a.tokens_out for a in attempts),
        "by_model": models,
        # 花在「最终没合并的任务」上的钱。降本的第一个靶子。
        "wasted_usd": round(
            sum(a.cost_usd for a in attempts if a.resolution != "merged"), 6
        ),
    }


def _gates(db: str | Path) -> dict:
    """判据表 + 一段主动的偏差声明。

    `caveat` 不是凑字数。OA 那套监控台的闸门学习页写了一句「回放样本全是
    正样本，所以任何放宽在回放上都会表现为无代价，数字会一致地夸奖放宽」——
    系统主动揭自己指标的系统性偏差，是那整套 UI 里最值钱的一句话。

    这里的同类偏差：`precision = TP/(TP+FP)`，而 unadjudicated（没人定案的
    触发）既不进分子也不进分母。当待定案很多时，precision 只反映了被定案的
    那一小部分，而人往往先去定案那些明显对的 —— 于是 precision 系统性偏高。
    照着这个数删判据会删错。所以每行都带 unadjudicated，并且顶层写明。
    """
    from factory.gate_claims import GATE_CLAIMS
    from factory.metrics import gate_metrics, supervisor_metrics

    store = AuditStore(db)
    gm = gate_metrics(store, known_gates=GATE_CLAIMS)
    sm = supervisor_metrics(store)

    rows = sorted(
        (
            {
                "gate_id": g.gate_id,
                "role": g.role,
                "gate": g.gate,
                "fired": g.fired,
                "true_positives": g.true_positives,
                "false_positives": g.false_positives,
                "unadjudicated": g.unadjudicated,
                "precision": (round(g.hit_rate, 4)
                              if g.hit_rate is not None else None),
                "verdict": g.verdict_line(),
            }
            for g in gm.values()
        ),
        # 从不触发的排最前：那才是需要人去看的一行。
        key=lambda r: (r["fired"] > 0, r["fired"]),
    )

    total_unadj = sum(r["unadjudicated"] for r in rows)
    total_adj = sum(r["true_positives"] + r["false_positives"] for r in rows)

    return {
        "rows": rows,
        "never_fired": sum(1 for r in rows if r["fired"] == 0),
        "roles": [
            {
                "role": role,
                "fired": m.fired,
                "passed": m.passed,
                "true_positives": m.true_positives,
                "false_positives": m.false_positives,
                "unadjudicated": m.unadjudicated,
                "precision": (round(m.hit_rate, 4)
                              if m.hit_rate is not None else None),
                "verdict": m.verdict_line(),
            }
            for role, m in sorted(sm.items())
        ],
        "caveat": {
            "unadjudicated": total_unadj,
            "adjudicated": total_adj,
            # 待定案比已定案还多时，这张表基本不能用来做裁剪决策。
            "reliable": total_adj > 0 and total_unadj <= total_adj,
            "text": (
                f"precision 的分母只含已定案的 {total_adj} 次触发，"
                f"另有 {total_unadj} 次待定案既不进分子也不进分母。"
                "人通常先定案明显正确的那些，所以这个数系统性偏高 —— "
                "在待定案清零之前，别照着它删判据。"
            ),
        },
    }


def _stuck(tasks_by_state: dict, attempts: tuple) -> list[dict]:
    """卡住的任务，按滞留时长倒序。

    只看 needs_human 和 blocked：inbox 里等派发是正常的（dispatcher 按节奏
    认领），running 长是任务本身重。真正的卡点是「等人而人没来」。
    """
    now = datetime.now()
    cost_by_task: defaultdict = defaultdict(float)
    runs_by_task: Counter = Counter()
    for a in attempts:
        cost_by_task[a.task_id] += a.cost_usd
        runs_by_task[a.task_id] += 1

    out = []
    for state in ("needs_human", "blocked"):
        for t in tasks_by_state.get(state) or []:
            mtime = t.get("mtime")
            days = None
            if mtime:
                try:
                    days = round(
                        (now - datetime.fromisoformat(mtime)).total_seconds() / 86400, 1
                    )
                except ValueError:
                    days = None
            out.append({
                "task_id": t["task_id"],
                "state": state,
                "preview": t.get("prompt_preview", ""),
                "stuck_days": days,
                "attempts": runs_by_task.get(t["task_id"], 0),
                "cost_usd": round(cost_by_task.get(t["task_id"], 0.0), 4),
            })
    return sorted(out, key=lambda r: -(r["stuck_days"] or 0))


def analytics(
    db: str | Path, queue: str | Path, *, window_days: int = 30
) -> dict:
    """总览页的全部数据，一次算完。

    只扫库一次（`all_attempts()` 带 selectinload 拉 supervisors），然后所有
    分块都吃这同一份 tuple。`_gates` 例外 —— 它走 metrics 模块，那边自己
    再扫一次；为省这一次扫库把 metrics 的聚合逻辑复制过来，代价是两份判据
    统计逻辑迟早不一致，不划算。

    队列读不到（`QueueUnreadable`）时不整体 500：审计侧的成本、趋势、判据表
    仍然有效。受影响的部分给 `None` 并在 `degraded` 里点名，前端据此显示
    「这块读不到」而不是「这块是空的」。
    """
    from factory.api import QueueUnreadable, list_tasks

    store = AuditStore(db)
    attempts = store.all_attempts()
    task_ids = {a.task_id for a in attempts}

    degraded: list[str] = []
    try:
        buckets = list_tasks(queue, db=db)
        queue_counts = {k: len(v) for k, v in buckets.items() if isinstance(v, list)}
        in_queue = {
            t["task_id"]
            for v in buckets.values() if isinstance(v, list)
            for t in v
        }
    except QueueUnreadable as exc:
        buckets = {}
        queue_counts = {}
        in_queue = set()
        degraded.append(f"队列目录读不到：{exc}")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_days": window_days,
        "core": _core(attempts, task_ids, window_days),
        "stages": _stage_counts(queue_counts, attempts, in_queue),
        "trend": _trend(attempts, window_days),
        "cost": _cost(attempts, task_ids),
        "gates": _gates(db),
        "stuck": _stuck(buckets, attempts),
        "resolutions": dict(Counter(a.resolution for a in attempts)),
        # 空态三分：从没跑过 / 读不到 / 有数据。前端不许把后两者显示成第一种。
        "empty": len(attempts) == 0 and not degraded,
        "degraded": degraded,
    }
