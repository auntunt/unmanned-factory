"""收件箱闭环：卡住的任务怎么看见、怎么放回去。

在这之前 `queue` 只给计数（needs-human 5），而计数回答不了人接下来唯一想问
的问题：哪 5 个、分别为什么。放回队列更是只能 `cp` 手搬 —— 而手搬会把
.result.json 一起带回 inbox，下一轮 park 时撞名。

这里的测试重点是**队列不变量**，不是输出好不好看：放回去的任务必须真能被
下一轮认领（否则 resolve 是个假动作），失败证据不能丢（下次再卡住要对比是
不是同一个原因），running 里的活条目不能被人硬搬（会和跑批抢同一个任务）。
"""

from __future__ import annotations

import json

import pytest

from factory.backlog.store import (
    BLOCKED,
    NEEDS_HUMAN,
    Backlog,
    BacklogError,
)


def make_task(tmp_path, tid: str):
    p = tmp_path / f"{tid}.yaml"
    p.write_text(f"id: {tid}\ngoal: demo\nacceptance:\n  - x\n", encoding="utf-8")
    return p


def park(b: Backlog, tmp_path, tid: str, outcome: str, note: str = ""):
    """走 add/claim/finish 把任务真的停到终态，不手工摆文件。

    手工摆文件的测试会在 park 的实现变了之后继续绿 —— 而那时候真实队列
    已经坏了。
    """
    added = b.add(make_task(tmp_path, tid))
    claim = b.claim(added)
    assert claim is not None
    return b.finish(claim, outcome, note=note)


# ---------- parked() ----------

def test_parked_reads_back_why_it_stopped(tmp_path):
    """列出条目时必须带上当初为什么停 —— 那是人来看这个列表的全部原因。"""
    b = Backlog(tmp_path / "q").ensure()
    park(b, tmp_path, "T-a", "escalated", note="3 轮未过：判据无法验证")
    (item,) = b.parked(NEEDS_HUMAN)
    assert item.task_id == "T-a"
    assert item.outcome == "escalated"
    assert "3 轮未过" in item.note
    assert item.result_ok
    assert item.finished_at > 0


def test_parked_separates_needs_human_from_blocked(tmp_path):
    """两个目录含义不同：一个等人改判据，一个是硬闸门永不无人执行。"""
    b = Backlog(tmp_path / "q").ensure()
    park(b, tmp_path, "T-esc", "escalated")
    park(b, tmp_path, "T-hard", "blocked_hard_gate")
    assert [i.task_id for i in b.parked(NEEDS_HUMAN)] == ["T-esc"]
    assert [i.task_id for i in b.parked(BLOCKED)] == ["T-hard"]


def test_an_item_with_no_result_json_is_still_listed(tmp_path):
    """缺 .result.json 的条目照样列出，标 result_ok=False。

    跳过它的话「目录里有 5 个但只列出 3 个」这种状态没人能发现。而缺 result
    恰恰最常见：手动扔进去的任务、park 中途被打断的条目。
    """
    b = Backlog(tmp_path / "q").ensure()
    dst = park(b, tmp_path, "T-a", "escalated", note="原因")
    dst.with_name(dst.name + ".result.json").unlink()
    (item,) = b.parked(NEEDS_HUMAN)
    assert item.task_id == "T-a"
    assert item.result_ok is False
    assert item.note == ""


def test_a_corrupt_result_json_does_not_hide_the_item(tmp_path):
    """坏掉的 JSON 同理 —— 人正是因为任务卡住了才来看这个列表。"""
    b = Backlog(tmp_path / "q").ensure()
    dst = park(b, tmp_path, "T-a", "escalated", note="原因")
    dst.with_name(dst.name + ".result.json").write_text("{ 这不是 json", encoding="utf-8")
    (item,) = b.parked(NEEDS_HUMAN)
    assert item.result_ok is False


def test_a_bad_timestamp_does_not_break_the_listing(tmp_path):
    """时间戳只用来显示「多久以前」，坏了不该让整张表列不出来。"""
    b = Backlog(tmp_path / "q").ensure()
    dst = park(b, tmp_path, "T-a", "escalated")
    rp = dst.with_name(dst.name + ".result.json")
    meta = json.loads(rp.read_text(encoding="utf-8"))
    meta["finished_at"] = "昨天"
    rp.write_text(json.dumps(meta), encoding="utf-8")
    (item,) = b.parked(NEEDS_HUMAN)
    assert item.finished_at == 0.0
    assert item.result_ok  # JSON 本身是好的，只是这个字段坏了


def test_result_and_claim_files_are_not_listed_as_tasks(tmp_path):
    """.result.json / .claim 是元数据，不是任务。列进去会让计数翻倍。"""
    b = Backlog(tmp_path / "q").ensure()
    park(b, tmp_path, "T-a", "escalated")
    assert len(b.parked(NEEDS_HUMAN)) == 1


# ---------- revive() ----------

def test_revive_task_is_claimable_again(tmp_path):
    """放回去的任务必须真能被下一轮认领。

    只把文件搬到 inbox/ 不算完：`claim_next()` 会跳过没通过依赖判定、或者
    被 .claim 旁文件占着的条目。搬完不验证「能被认领」的话，resolve 就是个
    假动作 —— 人以为放回去了，队列跑批看到的还是空的。
    """
    b = Backlog(tmp_path / "q").ensure()
    parked = park(b, tmp_path, "T-a", "escalated", note="3 轮未过")

    dst = b.revive(parked)

    assert dst.parent.name == "inbox"
    claim = b.claim_next()
    assert claim is not None and claim.task_id == "T-a"


def test_revive_archives_result_instead_of_dragging_it_to_inbox(tmp_path):
    """.result.json 必须留在原地改名，不能跟着任务回 inbox。

    反例（手搬时真实发生的）：`cp T-a.yaml*` 把 result 一起带回 inbox，
    下一轮 park 时 needs-human 里已经有一个同名 result，归档就撞上去了。
    """
    b = Backlog(tmp_path / "q").ensure()
    parked = park(b, tmp_path, "T-a", "escalated", note="判据无法验证")

    b.revive(parked)

    inbox = b.dir("inbox")
    assert [p.name for p in inbox.iterdir()] == ["T-a.yaml"]
    # 失败证据还在，只是改了名
    kept = parked.with_name(parked.name + ".result.json.1")
    assert kept.is_file()
    assert json.loads(kept.read_text(encoding="utf-8"))["note"] == "判据无法验证"


def test_revive_keeps_every_round_of_evidence(tmp_path):
    """同一个任务反复卡住时，每一轮的证据都要留下来。

    归档序号递增而不是覆盖：下次再卡住，唯一能判断「是不是同一个原因」的
    东西就是上一次的 note。覆盖掉等于每次都从零开始猜。
    """
    b = Backlog(tmp_path / "q").ensure()
    first = park(b, tmp_path, "T-a", "escalated", note="第一次：缺 check")
    b.revive(first)
    claim = b.claim_next()
    assert claim is not None
    second = b.finish(claim, "escalated", note="第二次：check 还是没过")
    b.revive(second)

    kept = sorted(p.name for p in b.dir(NEEDS_HUMAN).iterdir())
    assert kept == ["T-a.yaml.result.json.1", "T-a.yaml.result.json.2"]
    notes = [
        json.loads((b.dir(NEEDS_HUMAN) / n).read_text(encoding="utf-8"))["note"]
        for n in kept
    ]
    assert notes == ["第一次：缺 check", "第二次：check 还是没过"]


def test_revive_refuses_running_entries(tmp_path):
    """running 里的活条目不许硬搬 —— 会和跑批抢同一个任务。

    反例：人看到 running/ 里一个跑了很久的任务，手动搬回 inbox。原来那个
    进程还在跑并且会 finish，于是同一个任务被派发两次，两份 diff 落到同一
    个仓库上。
    """
    b = Backlog(tmp_path / "q").ensure()
    added = b.add(make_task(tmp_path, "T-a"))
    claim = b.claim(added)
    assert claim is not None

    with pytest.raises(BacklogError, match="只能从"):
        b.revive(claim.path)
    assert claim.path.is_file()          # 没被搬走


def test_revive_refuses_when_inbox_has_same_name(tmp_path):
    """inbox 里已有同名条目时拒绝，不静默覆盖。

    同名会真的发生：人重新 `factory prd` 生成了同一个 task_id 扔进 inbox，
    又去 resolve 老的那个。覆盖掉的话「跑批认领到哪一份」变成运气。
    """
    b = Backlog(tmp_path / "q").ensure()
    parked = park(b, tmp_path, "T-a", "escalated")
    b.add(make_task(tmp_path, "T-a"))    # 新版本已经在 inbox

    with pytest.raises(BacklogError, match="同名"):
        b.revive(parked)
    assert parked.is_file()


def test_revive_note_lands_next_to_the_task(tmp_path):
    """--why 的内容要落在 inbox 那份任务旁边，而不是只打在终端上。

    下一轮跑批可能是几小时后另一个人开的。终端输出早就滚没了，「上次为什么
    放回来」只能从这个旁文件读到。
    """
    b = Backlog(tmp_path / "q").ensure()
    parked = park(b, tmp_path, "T-a", "escalated")

    dst = b.revive(parked, note="改宽了 acceptance 的第 2 条")

    side = json.loads(
        dst.with_name(dst.name + ".revived.json").read_text(encoding="utf-8"))
    assert side["note"] == "改宽了 acceptance 的第 2 条"
    assert side["from"] == NEEDS_HUMAN
    assert side["revived_at"] > 0


def test_revive_works_from_blocked(tmp_path):
    """blocked（D 类硬闸门）也要能放回。

    blocked 的含义是「这类改动永不许无人跑」，但人自己执行完脚本、或者把
    任务改成不碰那片路径之后，它就该能回队列。没有出口的话 blocked/ 只会
    单调增长，最后没人再看。
    """
    b = Backlog(tmp_path / "q").ensure()
    parked = park(b, tmp_path, "T-a", "blocked_hard_gate", note="碰了 migrations/")
    assert parked.parent.name == BLOCKED

    dst = b.revive(parked, note="改成不碰 migrations/")

    assert dst.parent.name == "inbox"
    assert json.loads(
        dst.with_name(dst.name + ".revived.json").read_text(encoding="utf-8")
    )["from"] == BLOCKED


def test_revive_missing_path_says_someone_else_moved_it(tmp_path):
    """路径不在了要报「可能被别的进程搬走」，而不是 FileNotFoundError。

    两个人同时处理积压是常态。裸的 FileNotFoundError 会让人以为队列坏了，
    而实际情况是同事刚刚 resolve 了同一条。
    """
    b = Backlog(tmp_path / "q").ensure()
    with pytest.raises(BacklogError, match="不在了"):
        b.revive(b.dir(NEEDS_HUMAN) / "T-ghost.yaml")
