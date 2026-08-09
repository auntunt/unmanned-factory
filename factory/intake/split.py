"""一段口述需求 → N 条子需求 + 它们之间的依赖边。

**为什么加一层，而不是把 DRAFT_SCHEMA 改成数组：**

extract.py 的 `_parse` 里，guard 加固、checks 过滤、unclear 收集全是按「一条」
写的，而且 `harden_ops(original_desc, ...)` 拿的是**原始整段话**。改成数组之后
这些逻辑各自要长出一个循环，还要重新决定「加固时给它看整段还是子段」——
一个已经被实测覆盖过的路径会被整体改写。

分层之后每条子需求走的还是原来那条路：拆分器只产出「递给抽取器的那段话」，
抽取本身一个字节都没动。

代价明写：**N 条子任务 = N+1 次模型调用。** 所以 `--split` 不是默认开的 ——
默认拆分会让一句「加个重试」变成三个任务三份钱。

拆分器**不认识真 task_id**，只在 slug 之间连边。组 id 是调用方的事：拆分器
看不到队列里已有什么，让它编 id 等于让它猜一个可能撞名的身份。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass

from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.intake.extract import IntakeError, _MAX_FIELD

SPLIT_SCHEMA = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "description": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["slug", "description"],
            },
        },
    },
    "required": ["subtasks"],
}

_SYSTEM = """\
把用户的口头需求切成几件**独立可交付**的事。你只做切分，不做设计。

严格规则：
- 只在用户真的说了多件事时才切。**一件事的三个步骤不是三件事** —— 先写函数、
  再写测试、再跑一遍，这是一件事，交给下游的 agent 自己做。
- 判据：切出来的每一件都能单独提交、单独验收。做不到就不该切。
- description：把这一件事完整地重述一遍，**要能被单独读懂**。它会被单独递给
  下一个环节，那里看不到别的子任务，也看不到用户原话。别写「同上」「和前面
  一样」。
- depends_on：只填「B 必须等 A 合并了才能开始」这种真前置。
  顺序偏好不算依赖 —— 「我想先看到 A」不是 B 依赖 A。
  两件事改同一个文件也不算依赖，除非 B 要用 A 新加的东西。
  填的是别的 slug，不是描述文字。
- slug：短横线小写，形如 add-retry、fix-timeout。同一批里不许重复。

用户只说了一件事就返回一条。**宁可少切也不要硬切**：多切出来的任务每一条都
要单独花钱跑，而合在一起本来就能一次做完。
"""


@dataclass(frozen=True)
class SubTask:
    """一条待抽取的子需求。slug 只在这一批里唯一，不是最终 task_id。"""

    slug: str
    description: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class SplitResult:
    subtasks: tuple[SubTask, ...]
    #: 被丢掉的依赖边和原因。**不静默丢** —— 一条指向不存在前置的依赖会让
    #: 任务永远进 needs-human，那时已经离拆分很远了。
    dropped: tuple[str, ...] = ()
    tokens: int = 0
    cost_usd: float = 0.0

    @property
    def split(self) -> bool:
        """真拆出了多条。一条 = 退化成原来的行为。"""
        return len(self.subtasks) > 1


class TaskSplitter:
    """一次模型调用：整段需求 → SubTask 列表。

    和 TaskExtractor 同一条路线（subprocess + --json-schema + 空 cwd + 关工具）。
    关工具在这里防的是**替用户增加需求**：能读仓库的拆分器会看见「这里还该改
    一处」，然后切出一件用户没要求的事，而它下游每一条都会真的花钱去改代码。

    默认 haiku：这一跳只做切分，不判断对错，是整条链上最便宜的一步。
    """

    def __init__(
        self,
        *,
        binary: str = "claude",
        model: str = "haiku",
        timeout_s: int = 90,
    ) -> None:
        self._binary = binary
        self._model = model
        self._timeout_s = timeout_s

    def _argv(self, prompt: str) -> list[str]:
        return [
            self._binary, "-p", prompt,
            "--model", self._model,
            "--output-format", "json",
            "--tools", "",
            "--safe-mode",
            "--exclude-dynamic-system-prompt-sections",
            "--json-schema", json.dumps(SPLIT_SCHEMA),
        ]

    def run(self, description: str) -> SplitResult:
        from factory.redact import redact_text
        clean = redact_text(description[:_MAX_FIELD])
        prompt = f"{_SYSTEM}\n\n---\n用户说：\n{clean}"

        with tempfile.TemporaryDirectory(prefix="factory-split-") as cwd:
            try:
                proc = run_bounded(
                    self._argv(prompt), cwd=cwd, timeout_s=self._timeout_s,
                )
            except ProcTimeout:
                raise IntakeError(f"拆分超时（{self._timeout_s}s）")
            except OSError as exc:
                raise IntakeError(f"无法启动 {self._binary}: {exc}")

        return self._parse(proc.stdout or proc.stderr or "", description)

    def _parse(self, stdout: str, original: str) -> SplitResult:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            raise IntakeError(f"拆分器返回非 JSON：{stdout[:300]}")

        if payload.get("is_error"):
            detail = " ".join(str(payload.get(k, "")) for k in
                              ("subtype", "stop_reason", "result")).strip()
            raise IntakeError(f"拆分调用失败：{detail[:300]}")

        usage = payload.get("usage") or {}
        tokens = (int(usage.get("input_tokens", 0))
                  + int(usage.get("output_tokens", 0)))
        cost = float(payload.get("total_cost_usd") or 0.0)

        out = payload.get("structured_output")
        raw = (out or {}).get("subtasks") if isinstance(out, dict) else None
        if not isinstance(raw, list) or not raw:
            # 退化而不是抛：拆不出来就当「就是一件事」，走原来那条路。
            # 抛的话，一句本来就不该拆的需求会因为开了 --split 而整个失败。
            return SplitResult(
                subtasks=(SubTask(slug="", description=original),),
                dropped=("拆分器没返回 subtasks，按一条处理",),
                tokens=tokens, cost_usd=cost)

        subs, dropped = _normalize(raw)
        _reject_cycles(subs)
        return SplitResult(subtasks=subs, dropped=dropped,
                           tokens=tokens, cost_usd=cost)


def _normalize(raw: list) -> tuple[tuple[SubTask, ...], tuple[str, ...]]:
    """清洗 + 去重 slug + 丢掉指向未知 slug 的边（并说出来）。"""
    dropped: list[str] = []
    seen: dict[str, int] = {}
    cleaned: list[tuple[str, str, tuple[str, ...]]] = []

    for item in raw:
        if not isinstance(item, dict):
            continue
        slug = _slugify(str(item.get("slug", "")))
        desc = str(item.get("description", "")).strip()
        if not desc:
            dropped.append(f"{slug or '(无名)'}：描述为空，丢弃")
            continue
        if not slug:
            slug = f"sub-{len(cleaned) + 1}"
        if slug in seen:
            # 撞名不覆盖：覆盖会让先到的那条静默消失，而它的依赖边还指着这个
            # slug —— 于是一条真依赖会悄悄挂到另一件事上。
            seen[slug] += 1
            new = f"{slug}-{seen[slug]}"
            dropped.append(f"{slug}：slug 重复，改名为 {new}")
            slug = new
        seen.setdefault(slug, 1)
        deps = tuple(_slugify(str(d)) for d in (item.get("depends_on") or ()))
        cleaned.append((slug, desc, deps))

    known = {slug for slug, _, _ in cleaned}
    subs: list[SubTask] = []
    for slug, desc, deps in cleaned:
        keep = []
        for d in deps:
            if d == slug:
                dropped.append(f"{slug}：依赖自己，丢掉这条边")
            elif d not in known:
                # 指向不存在的前置 = 队列层的死锁。在这里丢掉并说出来，
                # 比让它半小时后在 needs-human 里冒出来便宜得多。
                dropped.append(f"{slug}：依赖的 {d!r} 不在这一批里，丢掉这条边")
            else:
                keep.append(d)
        subs.append(SubTask(slug=slug, description=desc,
                            depends_on=tuple(keep)))
    return tuple(subs), tuple(dropped)


def _slugify(raw: str) -> str:
    """只留小写字母数字和短横线。slug 会变成文件名和依赖键，不能带空格。"""
    s = "".join(c if c.isalnum() or c == "-" else "-" for c in raw.strip().lower())
    return "-".join(part for part in s.split("-") if part)


def _reject_cycles(subs: tuple[SubTask, ...]) -> None:
    """有环就抛。

    队列层也能抓到环（deadlocked()），但那要等到入队之后、人已经走开之后。
    在这里抛的话，人还在终端前面，重说一遍需求就完事了。
    """
    deps = {s.slug: s.depends_on for s in subs}
    state: dict[str, int] = {}          # 0=未访问 1=在栈上 2=完成

    def walk(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in deps.get(node, ()):
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt):] + [nxt] if nxt in path else [nxt]
                raise IntakeError(
                    "拆分结果里有循环依赖：" + " → ".join(cycle)
                    + "。这批任务谁都动不了 —— 把需求重说一遍，"
                      "或者手改 YAML 去掉一条边")
            if state.get(nxt, 0) == 0:
                walk(nxt, path + [nxt])
        state[node] = 2

    for s in subs:
        if state.get(s.slug, 0) == 0:
            walk(s.slug, [s.slug])
