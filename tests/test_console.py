"""对话式控制台的测试。

重点不在「HTML 里有没有那个 div」，而在**页面会不会撒谎**：

  - 队列读不出来时，显示的是「读不到」而不是「没有任务」
  - 队列目录是权威：journal 说在跑、文件已经在 done/，显示的必须是 done
  - 闸门内部码翻不出人话时，原样带出来而不是丢掉

这三条都是「静默错误」——页面看起来正常，内容是错的。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from factory.console import OK, STOP, WAIT
from factory.console_read import read_threads
from factory.console_render import page, threads_fragment


def _mkqueue(root: Path) -> Path:
    for d in ("inbox", "running", "done", "needs-human", "blocked", "log"):
        (root / d).mkdir(parents=True, exist_ok=True)
    return root


def _task(root: Path, state: str, task_id: str, prompt: str) -> Path:
    p = root / state / f"{task_id}.yaml"
    p.write_text(
        f"task_id: {task_id}\n"
        f"prompt: |\n  {prompt}\n"
        f"acceptance: []\nchecks: []\nmax_rounds: 3\n",
        encoding="utf-8",
    )
    return p


def _events(root: Path, evs: list[dict]) -> None:
    day = time.strftime("%Y-%m-%d")
    with (root / "log" / f"{day}.jsonl").open("w", encoding="utf-8") as f:
        for e in evs:
            e.setdefault("ts", time.time())
            e.setdefault("run_id", "r")
            e.setdefault("host", "h")
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ── 「读不到」和「空的」必须能区分 ────────────────────────────

def test_no_queue_configured_says_so():
    """没配队列时说清楚，不能渲染成一个空列表。

    空列表的意思是「你还没提过需求」，而实际是「服务器没配置」——
    用户会一直提需求然后发现什么都不出现。
    """
    threads, err = read_threads(None)
    assert threads == []
    assert err, "没配队列必须回一条 error"
    assert "队列" in err


def test_queue_path_missing_is_an_error_not_emptiness(tmp_path):
    """路径不存在 → error 非空。"""
    threads, err = read_threads(tmp_path / "nope")
    assert err, "读不到队列必须报错，不能静默返回空"


def test_empty_queue_is_not_an_error(tmp_path):
    """队列存在但是空的 → 没有 error。这是正常状态。"""
    threads, err = read_threads(_mkqueue(tmp_path))
    assert threads == []
    assert err == ""


def test_error_is_rendered_on_the_page(tmp_path):
    """error 必须出现在 HTML 里 —— 读不到队列的页面不能看着像正常的。"""
    html = page([], error="读队列失败：权限不足")
    assert "读队列失败" in html
    assert "权限不足" in html


# ── 队列目录是权威 ──────────────────────────────────────────

def test_queue_dir_wins_over_stale_journal(tmp_path):
    """journal 缺 dispatch 事件时，队列位置仍要显示对。

    journal 写失败不抛异常（是观测手段，不是任务的一部分），所以事件可能缺。
    如果只信 journal，一个已经 merged 的任务会永远显示「排队中」。
    """
    root = _mkqueue(tmp_path)
    _task(root, "done", "T-1", "加个按钮")
    # 只有 gate 事件，没有 dispatch —— 模拟日志写丢了
    _events(root, [{"kind": "gate", "task_id": "T-1", "admitted": True,
                    "codes": []}])

    threads, err = read_threads(root)
    assert err == ""
    assert len(threads) == 1
    t = threads[0]
    assert t.stage == "landed", f"文件在 done/，阶段必须是 landed，实际 {t.stage}"
    assert t.tone == OK
    assert t.done


def test_needs_human_shows_as_blocked(tmp_path):
    """needs-human 里的任务必须显示成「要你处理」，不能是「进行中」。"""
    root = _mkqueue(tmp_path)
    _task(root, "needs-human", "T-2", "删表")
    threads, _ = read_threads(root)
    assert threads[0].blocked
    assert threads[0].tone == STOP


def test_blocked_and_needs_human_say_different_things(tmp_path):
    """两者都要人介入，但要人做的事不一样，话术不能混。

    blocked = 这类改动永不许无人跑，你自己执行脚本
    needs-human = 跑过了没过验收，去看 diff
    """
    root = _mkqueue(tmp_path)
    _task(root, "blocked", "T-b", "改生产库")
    _task(root, "needs-human", "T-n", "加字段")
    threads, _ = read_threads(root)
    texts = {t.thread_id: t.steps[-1].text for t in threads}
    assert texts["T-b"] != texts["T-n"], "两种阻塞必须给不同的说明"
