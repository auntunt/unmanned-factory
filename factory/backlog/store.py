"""文件系统任务队列。

为什么是「目录 + 原子 rename」，不是 audit.db 里加一张表：

  - 队列的生产者不止工厂自己。`factory prd` 会写出 `tasks/<id>.yaml`，
    人会手改 YAML，脚本会 `cp` 进来。目录是这些东西唯一都会说的接口；
    多一张表就等于要求每个生产者都先学会连库。
  - 审计和调度的生命周期正相反：审计要永久留存，队列条目做完就该消失。
    绑在一个库里，清队列和保审计会开始互相打架。
  - crash 语义免费：同一文件系统上 rename 是原子的。进程被 kill 时，
    条目要么还在 inbox 要么已经在 running，不存在「读了一半」的中间态。

目录布局：

    <root>/inbox/        待派发
    <root>/running/      已认领（带 .claim 旁文件记 pid）
    <root>/done/         merged
    <root>/needs-human/  升级给人（3 轮未过 / 派发异常 / 崩溃残留）
    <root>/blocked/      D 类硬闸门，永不无人执行
    <root>/log/          循环日志（JSONL）
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

INBOX = "inbox"
RUNNING = "running"
DONE = "done"
NEEDS_HUMAN = "needs-human"
BLOCKED = "blocked"
LOG = "log"
STATES = (INBOX, RUNNING, DONE, NEEDS_HUMAN, BLOCKED)

CLAIM_SUFFIX = ".claim"
RESULT_SUFFIX = ".result.json"

#: `_park` 撞名时加的数字后缀（T-foo.2.yaml）。判依赖满足要剥掉它，
#: 否则「跑了第二遍才合并」的前置永远满足不了后继。
_PARK_SUFFIX = re.compile(r"\.\d+$")

# outcome（DispatchReport.outcome 的字符串值）→ 归档目录。
# blocked 和 needs-human 刻意分开：两者都要人介入，但要人做的事不一样 ——
# blocked 是「这类改动永不许无人跑，你自己执行脚本」，
# needs-human 是「跑过了没过验收，去看 diff」。混一个目录，`ls` 就分不出来了。
OUTCOME_DIR = {
    "merged": DONE,
    "escalated": NEEDS_HUMAN,
    "blocked_hard_gate": BLOCKED,
    "error": NEEDS_HUMAN,
}


class BacklogError(RuntimeError):
    """队列自身的错误（放错位置、同名冲突），不是任务验收失败。"""


@dataclass(frozen=True)
class Claim:
    """一次成功的认领。path 指向 running/ 下那份 YAML。"""

    path: Path
    claimed_at: float
    pid: int
    host: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def task_id(self) -> str:
        """给日志用的短名 = 去掉扩展名的文件名。

        不是 YAML 里的 task_id：认领时还没读文件，而认领日志必须先打出来 ——
        一个读 YAML 就崩掉的条目，正是最需要在日志里看到名字的那种。
        """
        return self.path.stem


@dataclass(frozen=True)
class Deadlock:
    """一个永远等不到前置的 inbox 条目。path 是它当前位置（还没搬走）。"""

    path: Path
    task_id: str
    missing: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Recovered:
    """一个从 running/ 里捞出来的崩溃残留。path 指向它现在的位置。"""

    path: Path
    task_id: str
    reason: str


def _is_task(path: Path) -> bool:
    """只认 .yaml/.yml。旁文件（.claim / .result.json）和杂物不算队列条目。"""
    return path.is_file() and path.suffix in (".yaml", ".yml")


def _alive(pid: int) -> bool:
    """pid 是否还在。只在同主机判断有意义，调用方负责先比 host。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于我们
    return True


class Backlog:
    """一个队列根目录上的全部操作。无状态：每次调用都看真实文件系统。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    # ---------- 目录 ----------

    def dir(self, state: str) -> Path:
        if state not in STATES and state != LOG:
            raise BacklogError(f"未知状态目录 {state!r}")
        return self.root / state

    def ensure(self) -> Backlog:
        for d in (*STATES, LOG):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        return self

    # ---------- 入队 ----------

    def add(self, task_yaml: str | Path, *, name: str | None = None) -> Path:
        """把一份任务 YAML 复制进 inbox。返回队列里的那个路径。

        复制而不是移动：源文件常常是 `factory prd` 的产物或人手写的模板，
        搬走它会让「我刚才写的那个文件呢」变成一次排查。
        """
        src = Path(task_yaml).expanduser()
        if not src.is_file():
            raise BacklogError(f"任务文件不存在：{src}")
        self.ensure()
        dst = self.dir(INBOX) / (name or src.name)
        if dst.exists():
            raise BacklogError(
                f"inbox 里已有同名条目：{dst.name}。"
                "改名再入队 —— 直接覆盖会让先排的那个静默消失"
            )
        dst.write_bytes(src.read_bytes())
        return dst

    def pending(self) -> tuple[Path, ...]:
        """inbox 里待派发的条目，按文件名排序。

        按名字而不是 mtime：mtime 会被 `cp` 重置，同一批入队的先后就没了。
        任务 id 本身带日期（T-20260806-...），名字序天然接近时间序；
        想插队就用 `00-` 前缀，这是刻意留的。
        """
        return self._entries(self.dir(INBOX))

    def running(self) -> tuple[Path, ...]:
        return self._entries(self.dir(RUNNING))

    def _entries(self, d: Path) -> tuple[Path, ...]:
        if not d.is_dir():
            return ()
        return tuple(sorted((p for p in d.iterdir() if _is_task(p)),
                            key=lambda p: p.name))

    # ---------- 依赖 ----------

    def merged_ids(self) -> frozenset[str]:
        """已合并的任务身份 = `done/` 里条目的文件名 stem（剥掉 `.N` 后缀）。

        判据是「在 done/ 里」，**不是「不在 inbox 里」**。后者会把
        needs-human 和 blocked 里的前置算成满足 —— 而那两个恰好是最需要人
        先看一眼的状态：前置没过验收就放后继去改同一片代码，等于把一个失败
        接着往下堆。

        只看文件名，不打开 YAML：一个读 YAML 就崩的条目正是最该被看见的那种，
        而在这里崩会让整个认领挂掉。
        """
        return frozenset(_PARK_SUFFIX.sub("", p.stem)
                         for p in self._entries(self.dir(DONE)))

    def missing_deps(self, path: Path, merged: frozenset[str] | None = None
                     ) -> tuple[str, ...]:
        """这个条目还缺哪几个前置。空元组 = 可以认领。

        读不出 YAML 时返回 ()，让它照常被认领 —— 坏 YAML 该由 dispatcher
        报错并归到 needs-human，在这里悄悄扣下它会让它变成一个永不派发、
        报表上又看不出来的条目。
        """
        if merged is None:
            merged = self.merged_ids()
        return tuple(d for d in self._declared_deps(path) if d not in merged)

    def _declared_deps(self, path: Path) -> tuple[str, ...]:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return ()
        if not isinstance(doc, dict):
            return ()
        return tuple(str(d) for d in (doc.get("depends_on") or ()))

    def blocked_by_deps(self) -> tuple[tuple[Path, tuple[str, ...]], ...]:
        """inbox 里因前置未合并而不可认领的条目，附上缺的前置。

        单独给一个查询而不是让 `claim_next()` 用返回值表达：`None` 已经背了
        「队列空」和「都被别人抢走了」两个意思。再压进「被前置挡住」的话，
        一批永远不跑的任务和一个跑空了的队列在日志里就长得一样 —— 这个仓库
        反复踩的正是这个形状。
        """
        merged = self.merged_ids()
        out = []
        for path in self.pending():
            missing = self.missing_deps(path, merged)
            if missing:
                out.append((path, missing))
        return tuple(out)

    def deadlocked(self) -> tuple[Deadlock, ...]:
        """inbox 里**永远等不到**前置的条目。

        判法是不动点：一个条目「终将可跑」当且仅当它的每个前置要么已经合并、
        要么在 running/（马上就有结果）、要么是另一个终将可跑的 inbox 条目。
        反复扫到集合不再增长，剩下的就是死锁 —— 环和「前置根本不在队列里」
        用同一个判据抓，不需要单独写一遍环检测。

        把 running/ 里的算成「终将满足」是刻意的：它可能失败进 needs-human，
        但那时它已经不在 running/ 了，下一轮扫描自然会把后继判成死锁。
        乐观一轮的代价是等一次 poll，悲观的代价是把正在跑的任务的后继误杀。
        """
        merged = self.merged_ids()
        in_flight = frozenset(_PARK_SUFFIX.sub("", p.stem)
                              for p in self.running())
        entries = {_PARK_SUFFIX.sub("", p.stem): p for p in self.pending()}
        deps = {name: self._declared_deps(p) for name, p in entries.items()}

        ok: set[str] = set()
        while True:
            grew = False
            for name, need in deps.items():
                if name in ok:
                    continue
                if all(d in merged or d in in_flight or d in ok for d in need):
                    ok.add(name)
                    grew = True
            if not grew:
                break

        out = []
        for name in sorted(set(entries) - ok):
            missing = tuple(
                d for d in deps[name]
                if not (d in merged or d in in_flight or d in ok)
            )
            out.append(Deadlock(
                path=entries[name], task_id=name, missing=missing,
                reason=self._dep_reason(name, missing, entries, deps, ok)))
        return tuple(out)

    def _dep_reason(self, name: str, missing: tuple[str, ...],
                    entries: dict[str, Path], deps: dict[str, tuple[str, ...]],
                    ok: set[str]) -> str:
        """逐个前置说清它现在在哪。

        「缺 B」「缺 B，B 自己也等不到」「和 B 成环」要人做的事完全不同：
        补一个任务 / 顺着链往上查 / 拆一个环。含糊成一句「可能成环」的话，
        人会先去找环，而链上根本没有环 —— 诊断方向被引偏。
        环用可达性真判，不猜：从 d 出发能回到 name 就是环。
        """
        parked = {state: {_PARK_SUFFIX.sub("", p.stem)
                          for p in self._entries(self.dir(state))}
                  for state in (NEEDS_HUMAN, BLOCKED)}
        parts = []
        for d in missing:
            if d in entries and d not in ok:
                where = ("和它成环" if self._reaches(d, name, deps)
                         else "也在 inbox 里，它自己也等不到前置")
            elif d in parked[NEEDS_HUMAN]:
                where = "在 needs-human，没过验收"
            elif d in parked[BLOCKED]:
                where = "在 blocked，D 类永不无人跑"
            else:
                where = "队列里根本没有这个条目"
            parts.append(f"{d}（{where}）")
        return "；".join(parts) or "无"

    @staticmethod
    def _reaches(start: str, target: str,
                 deps: dict[str, tuple[str, ...]]) -> bool:
        """沿 depends_on 从 start 能不能走到 target。带 seen 防自环卡死。"""
        seen: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(deps.get(cur, ()))
        return False

    # ---------- 认领 ----------

    def claim(self, path: Path) -> Claim | None:
        """inbox → running。抢不到（别的进程先手）返回 None，不抛异常。

        用 `os.link` + `unlink` 而不是 `os.rename`：rename 到已存在的目标会
        **静默覆盖**，两个进程同时认领同一个条目时双方都会以为自己赢了，
        任务被跑两遍。link 在目标已存在时抛 FileExistsError —— 输的一方
        能知道自己输了。
        """
        self.ensure()
        dst = self.dir(RUNNING) / path.name
        try:
            os.link(path, dst)
        except FileExistsError:
            return None       # 另一个 worker 已经认领
        except FileNotFoundError:
            return None       # 另一个 worker 刚把它搬走
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        claim = Claim(path=dst, claimed_at=time.time(), pid=os.getpid(),
                      host=socket.gethostname())
        self._write_claim(claim)
        return claim

    def claim_next(self) -> Claim | None:
        """认领 inbox 里第一个能抢到的条目。全被抢走则返回 None。

        循环而不是只试第一个：并发跑多个 loop 时，第一个总是被抢走的那个,
        只试一次会让后面的 worker 明明有活干却报 "队列空了"。

        前置未合并的条目被**跳过**（不是停下）：名字序在前的那个等着，不该
        挡住后面已经能跑的任务。判据和 `blocked_by_deps()` 是同一个
        （`missing_deps`），两边分头实现会让「跳过的」和「报出来的」对不上。
        """
        merged = self.merged_ids()
        for path in self.pending():
            if self.missing_deps(path, merged):
                continue
            got = self.claim(path)
            if got is not None:
                return got
        return None

    def _claim_file(self, task_path: Path) -> Path:
        return task_path.with_name(task_path.name + CLAIM_SUFFIX)

    def _write_claim(self, claim: Claim) -> None:
        self._claim_file(claim.path).write_text(
            json.dumps({"pid": claim.pid, "host": claim.host,
                        "claimed_at": claim.claimed_at}, ensure_ascii=False),
            encoding="utf-8",
        )

    def read_claim(self, task_path: Path) -> dict:
        f = self._claim_file(task_path)
        if not f.is_file():
            return {}
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    # ---------- 归档 ----------

    def finish(self, claim: Claim, outcome: str, *, note: str = "") -> Path:
        """running → done / needs-human / blocked，并写一份 .result.json。

        outcome 用的就是 `DispatchReport.outcome` 那套词（merged / escalated /
        blocked_hard_gate），外加一个循环自己造的 error。**队列层不自己发明
        一套状态名** —— 两套词之间的翻译表是这个文件里唯一一处 OUTCOME_DIR，
        多一层同义词只会让「done 到底对应哪个 outcome」变成一次查代码。

        **没有回 inbox 的路径**：重试是 dispatcher 内部的事（max_rounds），
        队列层再叠一层自动重试的话，一个必然失败的任务会无限烧钱，而且每一轮
        在审计里都长得像一个新任务。
        """
        state = OUTCOME_DIR.get(outcome)
        if state is None:
            raise BacklogError(
                f"未知 outcome {outcome!r}，只能是 {sorted(OUTCOME_DIR)}")
        dst = self._park(claim.path, state)
        dst.with_name(dst.name + RESULT_SUFFIX).write_text(
            json.dumps(
                {
                    "outcome": outcome,
                    "state": state,
                    "note": note,
                    "pid": claim.pid,
                    "host": claim.host,
                    "claimed_at": claim.claimed_at,
                    "finished_at": time.time(),
                    "wall_clock_s": round(time.time() - claim.claimed_at, 3),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return dst

    def park_deadlocked(self) -> tuple[Deadlock, ...]:
        """把死锁条目搬去 needs-human，各写一份 .result.json。

        **必须真的搬走。** 留在 inbox 里只打一行日志的话，一个永不被认领的
        条目和一个空队列在 `counts()`、在 `pending()` 的长度、在循环报表上
        全都长得一样 —— 于是「整夜没跑任何东西」会显示成「队列已抽干」。

        outcome 用现成的 `error`，不发明第六个状态目录：needs-human 的含义
        本来就是「要人看一眼」，死锁正是这个。
        """
        out = []
        for entry in self.deadlocked():
            moved = self._park(entry.path, NEEDS_HUMAN)
            # 返回的 path 指向**搬完之后**的位置。返回 inbox 里那个已经不存在的
            # 路径，会让调用方（循环、看板）拿着它去读文件时报「文件不存在」，
            # 而真正的原因是死锁 —— 诊断方向立刻被引偏。
            out.append(Deadlock(path=moved, task_id=entry.task_id,
                                missing=entry.missing, reason=entry.reason))
            moved.with_name(moved.name + RESULT_SUFFIX).write_text(
                json.dumps({"outcome": "error", "state": NEEDS_HUMAN,
                            "note": f"前置永远等不到：{entry.reason}",
                            "missing_deps": list(entry.missing),
                            "finished_at": time.time()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return tuple(out)

    def _park(self, task_path: Path, state: str) -> Path:
        """把条目搬到终态目录，同名时加数字后缀。

        同名会真的发生：同一个任务 YAML 跑第二遍（人改完 needs-human 里的
        判据后重新 add）时 basename 一样。覆盖掉的话第一次的失败证据就没了。
        """
        self.ensure()
        target = self.dir(state) / task_path.name
        if target.exists():
            stem, suffix = target.stem, target.suffix
            n = 2
            while target.exists():
                target = target.with_name(f"{stem}.{n}{suffix}")
                n += 1
        os.replace(task_path, target)
        claim_file = self._claim_file(task_path)
        if claim_file.is_file():
            claim_file.unlink()
        return target

    # ---------- 崩溃恢复 ----------

    def recover(self, *, stale_after_s: float = 6 * 3600.0
                ) -> tuple[Recovered, ...]:
        """把 running/ 里没人看管的条目搬去 needs-human。

        **不搬回 inbox。** 崩掉的那一轮可能已经烧掉了 token、可能已经在
        worktree 里留了一半的改动。自动重排等于允许重复计费和重复提交，
        而两者在审计里都长得像两个正常任务。让人看一眼再决定。

        判活优先于判龄：claim 里的 pid 在本机还活着就一律不动，哪怕它跑了
        一天 —— 一个还在跑的派发被当成僵尸抢走，结果是同一个任务被派两遍。
        只有在无法验证存活（没有 claim 文件、或 claim 来自别的机器）时才
        退回看时间。
        """
        out: list[Recovered] = []
        for path in self.running():
            reason = self._stale_reason(path, stale_after_s)
            if not reason:
                continue
            task_id = path.stem
            moved = self._park(path, NEEDS_HUMAN)
            moved.with_name(moved.name + RESULT_SUFFIX).write_text(
                json.dumps({"outcome": "error", "state": NEEDS_HUMAN,
                            "note": f"崩溃残留：{reason}",
                            "finished_at": time.time()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            out.append(Recovered(path=moved, task_id=task_id, reason=reason))
        return tuple(out)

    def _stale_reason(self, path: Path, stale_after_s: float) -> str:
        claim = self._claim_file(path)
        age = time.time() - path.stat().st_mtime
        if not claim.is_file():
            return "running/ 里没有 .claim 文件，无法判断归属"
        try:
            doc = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f".claim 文件读不出来：{exc}"
        if doc.get("host") != socket.gethostname():
            if age > stale_after_s:
                return (f"claim 来自另一台机器 {doc.get('host')!r}，"
                        f"且已静置 {age / 3600:.1f}h")
            return ""
        pid = int(doc.get("pid", 0) or 0)
        if pid and _alive(pid):
            return ""
        return f"claim 进程 pid={pid} 已不在"

    # ---------- 观察 ----------

    def counts(self) -> dict[str, int]:
        return {name: len(self._entries(self.dir(name))) for name in STATES}
