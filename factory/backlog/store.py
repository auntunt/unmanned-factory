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
import socket
import time
from dataclasses import dataclass
from pathlib import Path

INBOX = "inbox"
RUNNING = "running"
DONE = "done"
NEEDS_HUMAN = "needs-human"
BLOCKED = "blocked"
LOG = "log"
STATES = (INBOX, RUNNING, DONE, NEEDS_HUMAN, BLOCKED)

CLAIM_SUFFIX = ".claim"
RESULT_SUFFIX = ".result.json"

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
        """
        for path in self.pending():
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
