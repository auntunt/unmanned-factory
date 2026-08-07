"""跑批日志：一个无人循环留下的唯一现场。

为什么不是把终端输出重定向到文件就完了：

  - 终端输出是给盯着屏幕的人看的散文（「[2] deploy → 派发（已花 $0.0000）」）。
    第二天早上要回答的问题是「昨晚花了多少、有几个要我看、哪个任务最贵」——
    从散文里数这些要肉眼扫，而扫错了不会有任何提示。
  - cron 跑的循环，stdout 默认进邮件或者干脆丢掉。日志得由循环自己落盘，
    不能指望调用方记得加 `>> log`。

所以是 JSONL：一行一个事件，机器能 grep 能求和，人也还能读。和终端输出是**两个
消费者**，不共用一条通道 —— 合并的话，要么日志变成没法解析的散文，要么终端输出
变成没人想读的 JSON。

一个 run 的事件流：

    run_start → (recover)* → (dispatch)* → run_end

`run_end` 一定会写，包括异常退出的情况 —— 只有 run_start 没有 run_end 的日志，
本身就是「上次跑崩了」这个信息，比没有日志强。
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path


class Journal:
    """按天分文件的 append-only JSONL。

    按天分而不是一个大文件：无人循环是常驻的，一个月下来单文件会到几十 MB，
    而「看看昨晚」是最常见的查询。按 run 分又太碎 —— `--idle watch` 的循环
    一次能跑好几天。
    """

    def __init__(self, log_dir: str | Path) -> None:
        self.dir = Path(log_dir)
        self.run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
        self._host = socket.gethostname()

    def path_for(self, when: float | None = None) -> Path:
        day = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
        return self.dir / f"{day}.jsonl"

    def event(self, kind: str, **fields) -> None:
        """写一个事件。**写不进去不抛异常。**

        日志是观测手段，不是任务的一部分。磁盘满了、目录被人删了、权限变了——
        这些都不该让一个正在正常派发任务的循环停下来。反过来说，静默失败的
        日志确实可能让人以为「昨晚没跑」，所以这里往 stderr 抱怨一次。
        """
        rec = {"ts": time.time(), "run_id": self.run_id,
               "host": self._host, "kind": kind, **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.path_for(rec["ts"]).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:  # noqa: PERF203 - 见 docstring
            import sys
            print(f"[journal] 写日志失败（不影响派发）：{exc}",
                  file=sys.stderr)

    # ---------- 读回来 ----------

    def tail(self, *, limit: int = 20, kind: str | None = None
             ) -> tuple[dict, ...]:
        """读最近的事件，最新的在最后。

        跨文件读（按天分文件之后「最近 20 个」很可能横跨午夜）。坏行跳过而不是
        抛 —— 循环被 kill 时最后一行可能只写了一半，一个半行不该让 `queue
        --history` 整个不可用。
        """
        files = sorted(self.dir.glob("*.jsonl")) if self.dir.is_dir() else []
        out: list[dict] = []
        for path in reversed(files):
            rows = []
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind and rec.get("kind") != kind:
                    continue
                rows.append(rec)
            out = rows + out
            if len(out) >= limit:
                break
        return tuple(out[-limit:])


class Rollup:
    """一段时间里的汇总。给「昨晚跑得怎么样」这一个问题服务。

    刻意不做成通用查询：一个 SQL-ish 的过滤器接口会让这里长出第二套报表，
    而 spec §5.1 的报表已经在 audit.db 上了。这里只回答队列侧的问题——
    花了多少、有几个要人看、最贵的是哪个、循环有没有非正常退出。
    """

    def __init__(self, events: tuple[dict, ...]) -> None:
        self.runs = [e for e in events if e.get("kind") == "run_end"]
        self.starts = [e for e in events if e.get("kind") == "run_start"]
        self.tasks = [e for e in events if e.get("kind") == "dispatch"]
        self.recovered = [e for e in events if e.get("kind") == "recover"]

    @property
    def cost_usd(self) -> float:
        return sum(float(e.get("cost_usd") or 0.0) for e in self.tasks)

    @property
    def needs_human(self) -> tuple[dict, ...]:
        """要人介入的任务。escalated + blocked + error 三种都算。

        三者要人做的事不一样，但「明天早上要看几个」是同一个数。分开的明细
        在 needs-human/ 和 blocked/ 目录里。
        """
        return tuple(e for e in self.tasks
                     if e.get("outcome") != "merged")

    @property
    def priciest(self) -> dict | None:
        paid = [e for e in self.tasks if float(e.get("cost_usd") or 0.0) > 0]
        return max(paid, key=lambda e: float(e["cost_usd"])) if paid else None

    @property
    def crashed_runs(self) -> int:
        """有 run_start 但没有对应 run_end 的次数。

        这是「循环自己崩了」的唯一信号 —— 崩掉的进程没机会写 run_end，
        也没机会报警。不数出来的话，一个每晚都在启动时崩掉的 cron
        看起来和一个每晚都无事可做的 cron 一模一样。
        """
        ended = {e.get("run_id") for e in self.runs}
        return sum(1 for e in self.starts if e.get("run_id") not in ended)

    def lines(self) -> list[str]:
        out = [f"任务 {len(self.tasks)} 个，花费 ${self.cost_usd:.4f}，"
               f"待人介入 {len(self.needs_human)} 个"]
        if self.recovered:
            out.append(f"崩溃残留回收 {len(self.recovered)} 个")
        if self.crashed_runs:
            out.append(f"⚠ 循环非正常退出 {self.crashed_runs} 次"
                       f"（有 run_start 无 run_end）")
        if (p := self.priciest):
            out.append(f"最贵：{p.get('task_id')} "
                       f"${float(p['cost_usd']):.4f} → {p.get('outcome')}")
        for e in self.needs_human[:10]:
            out.append(f"  [{e.get('outcome')}] {e.get('task_id')}  "
                       f"{str(e.get('note') or '')[:90]}")
        if len(self.needs_human) > 10:
            out.append(f"  … 另有 {len(self.needs_human) - 10} 个")
        return out
