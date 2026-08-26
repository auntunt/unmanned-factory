"""Web 侧的人工介入动作：定案、验收、重跑。

## 为什么这是个新文件而不是往 api.py 里塞

`api.py` 顶上那段写权限边界写得很清楚，`route_post` 的签名**故意**不收 `db`，
为的是让「POST 不碰审计库」在类型上就成立。那个约束现在要被推翻 —— 人工定案
的结果必须落审计，否则 metrics 里永远躺着一片 pending，监工命中率算不出来。

推翻一条刻意设下的约束，代价应该是显式的：新开一个模块，把所有写审计库的
动作圈在这里，`api.py` 只负责把 POST 转进来。这样「哪些代码能写 audit.db」
仍然是一个可以一眼看完的清单，而不是散进 700 行的路由表里。

## 三个动作，和 CLI 一一对应

    POST /api/task/<id>/resolve   ← factory resolve <id> merged|reworked --why
    POST /api/task/<id>/accept    ← 人工验收（CLI 没有，Web 独有）
    POST /api/task/<id>/rerun     ← factory resolve <id> reworked --requeue

resolve 和 rerun 不是重复：多数时候人核过之后认可这一轮（定案，任务不该再跑），
只有改了判据才要重跑。CLI 用 `--requeue` 开关表达，Web 用两个按钮 —— 按钮上
写着后果比一个 checkbox 清楚。

## 不可协商的次序：审计先落，队列后搬

抄 CLI 的 `_cmd_resolve`，理由在那儿写着：反序的话中途失败会留下一个已经回到
inbox、下一轮就被重新认领的任务，而审计里这一轮还挂着 pending —— 同一个任务
出现两条没定案的 attempt，命中率的分母再也对不上。

## 幂等

同一个按钮被连点两次（网络慢时人一定会这么做）不能产生两条定案。`finalize`
本身是幂等的（写同一个值），但 `revive` 不是 —— 第二次会因为 inbox 里已有
同名文件而 BacklogError。那个报错要翻成一句人能懂的话，不是 500。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from factory.audit.models import Resolution
from factory.audit.store import AuditStore
from factory.backlog.store import (
    BLOCKED,
    Backlog,
    BacklogError,
    NEEDS_HUMAN,
    base_task_id,
)

#: Web 允许的定案词。和 CLI 的 `_RESOLVE_WORDS` 是同一份语义，故意不 import
#: 那个私有常量 —— cli.py 会为了别的原因改它，而 Web 的按钮集合应该独立演进。
#: 两边都只开放这两个：pending / escalated 是系统写的中间态，让人手填会把
#: metrics 的「未定案」搅成噪声。
RESOLVE_WORDS: dict[str, Resolution] = {
    "merged": Resolution.MERGED,
    "reworked": Resolution.REWORKED,
}

#: 理由的长度上限。库里那一列是 VARCHAR(512)，`finalize` 会截断 —— 但让
#: 前端先看到「你写太长了」比默默截掉 300 字好，那 300 字是人打的。
NOTE_MAX = 512


@dataclass(frozen=True)
class ActionOutcome:
    """一次人工动作的结果。`code` 直接当 HTTP 状态码用。"""

    code: int
    payload: dict


def _bad(msg: str, **extra) -> ActionOutcome:
    """400：调用方给的东西不对，改了参数再来。"""
    return ActionOutcome(400, {"error": msg, **extra})


def _clean_note(raw) -> tuple[str, str | None]:
    """理由的校验。返回 `(净化后的理由, 报错)`。

    人工介入**必须**写理由，这是这个系统存在的前提之一：两周后发现某个监工
    假阳性一堆，唯一能判断「是监工不行还是人图省事直接放行」的东西就是这一列。
    空理由的定案等于没有审计价值，所以这里挡住，不给默认值。
    """
    note = str(raw or "").strip()
    if not note:
        return "", "必须写定案理由 —— 没有理由的人工介入在审计里没有价值"
    if len(note) > NOTE_MAX:
        return "", f"理由 {len(note)} 字，超过 {NOTE_MAX} 上限（库里那一列存不下）"
    return note, None


def _find_parked(queue: str | Path, task_id: str) -> tuple[object | None, str | None]:
    """在 needs-human / blocked 里找这个任务的队列条目。

    返回 `(条目, 报错)`。条目为 None 且无报错 = 任务不在这两个目录里，
    这是合法状态：任务可能已经 merged 走了，人只是想补一条定案理由。
    那种情况下审计能落，队列无事可做。
    """
    b = Backlog(queue)
    hits: list = []
    for state in (NEEDS_HUMAN, BLOCKED):
        d = b.dir(state)
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            # 只认任务 YAML。.result.json / .revived.json 是附属文件。
            if p.suffix not in (".yaml", ".yml"):
                continue
            # 队列里的名字带 park 后缀（T-x.3），审计里是 YAML 里那个 ID。
            # 剥掉后缀才能对上。
            if base_task_id(p.stem) == base_task_id(task_id):
                hits.append((state, p))

    if not hits:
        return None, None
    if len(hits) > 1:
        # 同名归档（T-x.2、T-x.3）会撞车。CLI 让人指全名；Web 上人只有一个
        # 任务 ID 可点，没法指定 —— 所以挡住并说清楚，让他去 CLI 处理。
        names = ", ".join(p.name for _, p in hits)
        return None, (
            f"{task_id} 在队列里对上了 {len(hits)} 个条目（{names}）。"
            f"Web 上没法指定是哪一个，用 CLI：factory resolve <文件名> ..."
        )
    return hits[0], None


def do_resolve(
    task_id: str, data: dict, *, db: str | Path, queue: str | Path
) -> ActionOutcome:
    """人工定案。落审计，**不动队列** —— 要重跑走 do_rerun。

    对应 `factory resolve <id> <merged|reworked> --why '...'`（不带 --requeue）。
    """
    word = str(data.get("resolution") or "").strip()
    if word not in RESOLVE_WORDS:
        return _bad(
            f"resolution 只能是 {' / '.join(RESOLVE_WORDS)}，收到 {word!r}",
            allowed=sorted(RESOLVE_WORDS),
        )

    note, err = _clean_note(data.get("note"))
    if err:
        return _bad(err)

    audit_id = base_task_id(task_id)
    store = AuditStore(db)
    row = store.latest_attempt(audit_id)
    if row is None:
        # 没有 attempt 是正常情况：死锁 park、手动扔进来的任务从没派发过。
        # 但必须说出来 —— 不然人会以为 metrics 里该出现这条定案。
        return ActionOutcome(409, {
            "error": f"审计库里没有 {audit_id} 的 attempt（没派发过就卡住了？）",
            "hint": "这个任务从没跑过，没有可定案的轮次。要它跑就用「放回队列」",
        })

    store.finalize(row.id, RESOLVE_WORDS[word], note=note)
    return ActionOutcome(200, {
        "ok": True,
        "task_id": audit_id,
        "attempt_no": row.attempt_no,
        "attempt_id": row.id,
        "resolution": RESOLVE_WORDS[word].value,
        "note": note,
        "message": f"attempt #{row.attempt_no} 已定案为 {word}。任务留在原队列，要它重跑用「放回队列」",
    })


def do_rerun(
    task_id: str, data: dict, *, db: str | Path, queue: str | Path
) -> ActionOutcome:
    """定案为 reworked 并放回 inbox 重跑。

    对应 `factory resolve <id> reworked --why '...' --requeue`。

    定案词固定 reworked，不让前端传：会放回去重跑，就是承认这一轮的红是
    真阳性（判据或代码要改）。允许传 merged + requeue 会造出「已合并但又
    在跑」的矛盾状态，metrics 那边没法解释。
    """
    note, err = _clean_note(data.get("note"))
    if err:
        return _bad(err)

    audit_id = base_task_id(task_id)
    found, err = _find_parked(queue, task_id)
    if err:
        return _bad(err)
    if found is None:
        return ActionOutcome(409, {
            "error": f"{task_id} 不在 needs-human/ 或 blocked/ 里，没有可放回的条目",
            "hint": "running/ 里的活条目要等它自己结束或超时回收，硬搬会让跑批和你抢同一个任务",
        })
    state, path = found  # type: ignore[misc]

    # 审计先落，队列后搬。反序的话中途失败会留下一个已回到 inbox、下一轮就被
    # 重新认领的任务，而审计里这一轮还挂着 pending。
    store = AuditStore(db)
    row = store.latest_attempt(audit_id)
    audit_msg = ""
    if row is None:
        audit_msg = f"⚠ 审计库里没有 {audit_id} 的 attempt，这次只动队列"
    else:
        store.finalize(row.id, Resolution.REWORKED, note=note)
        audit_msg = f"attempt #{row.attempt_no} 已定案为 reworked"

    try:
        b = Backlog(queue)
        b.revive(path, note=note)
    except BacklogError as exc:
        # 审计已经落了，队列没搬动。这不是需要回滚的状态：定案本身是对的，
        # 人重试时 finalize 会幂等地写同一个值。但要说清楚现在是什么状态，
        # 否则人会以为整个操作失败了而去重复点。
        return ActionOutcome(409, {
            "error": f"定案已落审计，但放不回队列：{exc}",
            "audit_done": row is not None,
            "hint": "定案已生效。队列问题解决后再点一次「放回队列」，定案不会重复",
        })

    return ActionOutcome(200, {
        "ok": True,
        "task_id": audit_id,
        "resolution": Resolution.REWORKED.value,
        "note": note,
        "from_state": state,
        "message": f"{audit_msg}；任务已从 {state}/ 放回 inbox/，下一轮跑批会重新认领",
    })


#: 验收结论 → 中文说法。只在响应消息里用，库里存的是英文值。
_VERDICT_CN = {"pass": "通过", "fail": "不通过"}


def do_accept(
    task_id: str, data: dict, *, db: str | Path, queue: str | Path
) -> ActionOutcome:
    """人工验收：产出的东西人认不认。

    CLI 没有对应命令 —— 这是 Web 独有的动作。理由：验收要看 diff 正文和
    transcript，那是个读几百行的动作，终端里翻不动。

    验收**不改 resolution**，也不动队列。它回答的是另一个问题（活干得好不好），
    跟「监工那条红准不准」正交。所以一轮可以 resolution=merged 同时验收 fail，
    那种组合恰恰是最有价值的信号：检查全绿但人不认，说明判据写窄了。
    """
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in _VERDICT_CN:
        return _bad(
            f"verdict 只能是 pass / fail，收到 {verdict!r}",
            allowed=["pass", "fail"],
        )

    # 验收说明的上限是 1024（比定案理由宽）：不通过时人要写清楚哪儿不对，
    # 512 字经常不够。
    note = str(data.get("note") or "").strip()
    if not note:
        return _bad("验收必须写说明 —— 只有结论没有原因，下一个人得重读全部 diff")
    if len(note) > 1024:
        return _bad(f"说明 {len(note)} 字，超过 1024 上限")

    audit_id = base_task_id(task_id)
    store = AuditStore(db)

    # 允许指定轮次：人可能在看第 2 轮的 diff 时下结论，而这时已经跑到第 3 轮。
    # 不给 attempt_no 就落在最后一轮（前端默认行为）。
    raw_no = data.get("attempt_no")
    if raw_no in (None, ""):
        row = store.latest_attempt(audit_id)
    else:
        try:
            want = int(raw_no)
        except (TypeError, ValueError):
            return _bad(f"attempt_no 得是整数，收到 {raw_no!r}")
        rows = [r for r in store.attempts_for(audit_id) if r.attempt_no == want]
        row = rows[0] if rows else None
        if row is None:
            return ActionOutcome(404, {
                "error": f"{audit_id} 没有第 {want} 轮",
                "available": [r.attempt_no for r in store.attempts_for(audit_id)],
            })

    if row is None:
        return ActionOutcome(409, {
            "error": f"审计库里没有 {audit_id} 的 attempt，没有可验收的轮次",
            "hint": "这个任务从没派发过，没有产出可验收",
        })

    try:
        store.accept(row.id, verdict, note=note)
    except (ValueError, KeyError) as exc:
        # store.accept 的校验和上面重了一层，这里兜住是因为那一层是数据完整性
        # 的最后防线，不该靠调用方自觉。翻成 400 而不是 500：是入参的问题。
        return _bad(str(exc))

    return ActionOutcome(200, {
        "ok": True,
        "task_id": audit_id,
        "attempt_no": row.attempt_no,
        "attempt_id": row.id,
        "verdict": verdict,
        "note": note,
        "message": f"attempt #{row.attempt_no} 人工验收：{_VERDICT_CN[verdict]}",
    })
