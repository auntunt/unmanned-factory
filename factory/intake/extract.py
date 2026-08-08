"""自由文本 → 结构化 Task。模型只管把话变成结构，分级的守门人是 guard。

和 ClaudeJudge 同一条路线（subprocess + --json-schema + 空 cwd），理由也
大半相同，但有一条是这里独有的：

  **`--tools ""` 在这里防的是编造路径。**
  监工那边关工具是为了独立性（够不到没递给它的输入）。这里关工具是因为
  抽取器一旦能读仓库，就会把"看起来该改的文件"写进 declared_paths，
  而 declared_paths 是分级引擎的输入 —— 等于让它顺手替用户做了分级决定。
  它该做的只是把用户说出口的文件名抄下来。用户没说，就留空，让
  diff 后的第二次分级（spec §4 的风险监工）按真实改动来判。

checks 也一样只抄用户说的验收标准。它不知道这个仓库怎么跑测试，
猜出来的 `npm test` 会让回归监工判 fail 而根本不是 agent 的问题。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.intake.guard import GuardFinding, harden_ops

_MAX_FIELD = 4000

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "prompt": {"type": "string"},
        "spec_ref": {"type": "array", "items": {"type": "string"}},
        "acceptance": {"type": "array", "items": {"type": "string"}},
        "declared_paths": {"type": "array", "items": {"type": "string"}},
        "declared_ops": {"type": "array", "items": {"type": "string"}},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "command": {"type": "string"},
                    "expect": {
                        "type": "string",
                        "enum": ["exit_zero", "stdout_contains", "commands_agree"],
                    },
                    "value": {"type": "string"},
                },
                "required": ["name", "command"],
            },
        },
        "unclear": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["task_id", "prompt"],
}

_SYSTEM = """\
把用户的口头需求转成一条结构化任务。你只做转写，不做设计。

严格规则：
- prompt：用户想让 agent 做什么，写清楚、可执行。不要替用户增加需求。
- declared_paths：**只填用户明确说到的文件路径**。用户没说文件名就留空数组。
  不要推测、不要凭常见项目结构补全。
- declared_ops：只填用户明确要求的不可逆操作，可选值：
  prod_deploy data_delete schema_migration force_push drop_table truncate
  registry_push new_ux visual_change
  没有就留空数组。
- checks：**只填用户说出来的验收方式**。用户没说怎么验就留空数组。
  不要猜测试命令（你不知道这个仓库怎么跑测试）。
- spec_ref：用户提到的**已有文档**的编号 / 章节（如 AC-1、§3.2），没有就留空。
- acceptance：把用户这段话里的要求拆成可逐条核对的验收标准，一条一句。
  这是**必填**的实质字段 —— 口述需求没有外部文档，它本身就是规格。
  只写用户真正要求的，不要加他没提的标准。每条都要能对着代码判断真假：
  好：「truncate_words 词数超过 n 时返回前 n 个词并以 '...' 结尾」
  坏：「函数实现正确」「代码质量良好」
- task_id：短横线小写标识，形如 T-add-retry-1。
- unclear：用户没讲清、需要人确认的点。有疑问放这里，**不要**在 prompt 里替他猜。

宁可留空也不要编。空字段下游有兜底，编出来的字段会让分级判错。
"""


class IntakeError(RuntimeError):
    """抽取失败。不给残缺草稿 —— 半个 Task 派发出去比没有更糟。"""


@dataclass
class DraftTask:
    """待人确认的草稿。**不是** Task —— 中间多一步是故意的。

    入口层的产物必须先落成 YAML 给人看一眼，再 `factory run`。
    直接从一段话接进 dispatcher 就等于把"我想改点东西"变成了自动派发，
    分级的输入（paths / ops）也就没人核对过了。
    """

    task_id: str
    prompt: str
    spec_ref: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    declared_paths: tuple[str, ...] = ()
    declared_ops: tuple[str, ...] = ()
    checks: tuple[dict, ...] = ()
    unclear: tuple[str, ...] = ()
    guard_findings: tuple[GuardFinding, ...] = ()
    source_text: str = ""
    tokens: int = 0
    cost_usd: float = 0.0

    def to_yaml(self) -> str:
        doc: dict = {
            "task_id": self.task_id,
            "prompt": self.prompt.rstrip() + "\n",
            "spec_ref": list(self.spec_ref),
            "acceptance": list(self.acceptance),
            "declared_paths": list(self.declared_paths),
            "declared_ops": list(self.declared_ops),
            "max_rounds": 3,
            "checks": [dict(c) for c in self.checks],
        }
        body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=88)
        return _header(self) + body

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_yaml(), encoding="utf-8")
        return p


def _header(draft: DraftTask) -> str:
    """把 guard 的判定和模型的疑问写进 YAML 注释。

    这些是给人看的，所以放注释而不是字段：Task.from_yaml 忽略它们，
    但打开文件的人第一眼就看到"为什么这条被判成了 D 类"。
    """
    lines = ["# 由 `factory prd` 生成，**未经人工确认**。看过再 factory run。"]
    if not draft.acceptance and not draft.spec_ref:
        # 空 acceptance 会让规格监工判 fail（无标准可核 ≠ 核过了）。
        # 那个失败发生在派发一轮之后、花掉钱之后，报出来又只是一句
        # 「监工不可用」—— 在这里说清楚，比让人去猜便宜得多。
        lines.append("#")
        lines.append("# ⚠ acceptance 为空。开 --spec-review 会判 fail："
                     "无标准可核不等于核过了。")
        lines.append("#   要么在下面 acceptance 里补上可逐条核对的标准，"
                     "要么别开 --spec-review。")
    if draft.unclear:
        lines.append("#")
        lines.append("# 模型认为没讲清的点：")
        lines += [f"#   - {u}" for u in draft.unclear]
    if draft.guard_findings:
        lines.append("#")
        lines.append("# guard 补的 declared_ops（关键词扫描，只增不减）：")
        lines += [f"#   - {f.line()}" for f in draft.guard_findings]
        lines.append("#   这些 op 会把任务判成 C/D 类。C 类永不无人，"
                     "D 类是硬闸门（只出脚本，不代为执行）。")
        lines.append("#   误判就手动删掉对应行 —— 但请确认你真的不做这件事。")
    return "\n".join(lines) + "\n\n"


class TaskExtractor:
    """一次模型调用：自由文本 → DraftTask。

    重用 ClaudeJudge 的 subprocess 路线，理由见模块注释。
    binary 可以替换为 fake harness，便于测试（不调真模型）。
    """

    def __init__(
        self,
        *,
        binary: str = "claude",
        model: str = "sonnet",
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
            "--tools", "",                          # 不让它读仓库
            "--safe-mode",
            "--exclude-dynamic-system-prompt-sections",
            "--json-schema", json.dumps(DRAFT_SCHEMA),
        ]

    def run(self, description: str) -> DraftTask:
        """description → DraftTask（含 guard 补充）。"""
        from factory.redact import redact_text
        clean_desc = redact_text(description[:_MAX_FIELD])
        prompt = f"{_SYSTEM}\n\n---\n用户说：\n{clean_desc}"

        with tempfile.TemporaryDirectory(prefix="factory-intake-") as cwd:
            try:
                proc = run_bounded(
                    self._argv(prompt), cwd=cwd, timeout_s=self._timeout_s,
                )
            except ProcTimeout:
                raise IntakeError(f"模型提取超时（{self._timeout_s}s）")
            except OSError as exc:
                raise IntakeError(f"无法启动 {self._binary}: {exc}")

        return self._parse(proc.stdout or proc.stderr or "", description)

    def _parse(self, stdout: str, original_desc: str) -> DraftTask:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            raise IntakeError(f"模型返回非 JSON：{stdout[:300]}")

        usage = payload.get("usage") or {}
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        cost = float(payload.get("total_cost_usd") or 0.0)

        if payload.get("is_error"):
            detail = " ".join(str(payload.get(k, "")) for k in
                              ("subtype", "stop_reason", "result")).strip()
            raise IntakeError(f"模型调用失败：{detail[:300]}")

        out = payload.get("structured_output")
        if not isinstance(out, dict) or not out.get("task_id"):
            raise IntakeError(f"模型未返回 task_id：{str(out)[:300]}")

        raw_ops = [str(o) for o in out.get("declared_ops") or []]
        ops, findings = harden_ops(original_desc, raw_ops)

        checks: tuple[dict, ...] = tuple(
            {k: v for k, v in {
                "name": str(c.get("name", "")),
                "command": str(c.get("command", "")),
                "expect": c.get("expect"),
                "value": c.get("value"),
            }.items() if v}
            for c in (out.get("checks") or [])
            if isinstance(c, dict) and c.get("name") and c.get("command")
        )

        return DraftTask(
            task_id=str(out.get("task_id", "")).strip(),
            prompt=str(out.get("prompt", "")).strip(),
            spec_ref=tuple(str(s) for s in (out.get("spec_ref") or [])),
            acceptance=tuple(
                s for s in (str(a).strip() for a in (out.get("acceptance") or []))
                if s
            ),
            declared_paths=tuple(str(p) for p in (out.get("declared_paths") or [])),
            declared_ops=ops,
            checks=checks,
            unclear=tuple(str(u) for u in (out.get("unclear") or [])),
            guard_findings=findings,
            source_text=original_desc,
            tokens=tokens,
            cost_usd=cost,
        )
