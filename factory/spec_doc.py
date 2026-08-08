"""把 spec_ref 里的编号（AC-1、§3.2）解析成规格文档里的正文。

**为什么需要它。** `spec_ref` 原来是原样进规格监工 prompt 的。也就是说一个
写着 `spec_ref: [AC-1]` 的任务，监工看到的验收标准字面上就是一行 `- AC-1` ——
AC-1 要求什么它无从知道。真跑过一次，监工自己看出来了（「AC-1 只是一个编号
标签，没有任何具体描述文本」），但它判的是 **fail**，而那条 claim 不带
`supervisor-` 前缀，于是被 dispatcher 当成真问题**打回 worker** —— worker 改不了
「AC-1 没有正文」这件事，三轮烧完升级给人。spec §7.3 把 PRD ↔ diff 一致性
叫做「本项目真正的差异化」，而它的实际状态是一个永远悬空的字符串。

**为什么是确定性解析而不是调模型。** 抽取会出错，而**抽错的标准比没有标准更
危险**：监工会拿着一条不存在的要求去判 diff，而且判得理直气壮 —— 没有任何
下游能发现那条标准是编出来的。所以这里只认几种明确的行首写法，抽不到就返回
None 让上游拦住，把判断交回给人。和 `intake/guard.py`、`runbook` 同一个取舍。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: 单条正文的上限。规格里一条验收标准不该有一整章那么长，超了大概是
#: 匹配串到了下一节 —— 截断比把 40k 字塞进监工 prompt 好。
_MAX_BODY = 2000

#: 文档大小上限。防的是误指向一个生成物（lock 文件、构建日志）。
_MAX_DOC = 400_000


@dataclass(frozen=True)
class Resolved:
    """一次解析的结果。

    found 和 missing 都留着，因为上游要区别对待：全找到才放行，
    而报错时人需要知道**哪几条**没找到 —— 「AC-3 不在文档里」是可以动手
    修的信息，「解析失败」不是。
    """

    #: 编号 → 正文（含编号本身）。顺序和请求的 spec_ref 一致。
    found: tuple[tuple[str, str], ...] = ()
    #: 在文档里找不到的编号
    missing: tuple[str, ...] = ()
    #: 文档本身的问题（不存在、读不了、太大）。有值时 found 一定为空。
    doc_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.doc_error

    @property
    def bodies(self) -> tuple[str, ...]:
        return tuple(body for _, body in self.found)


def _patterns(ref: str) -> tuple[re.Pattern[str], ...]:
    """一个编号的几种常见行首写法。

    编号里可能有正则元字符（`§3.2` 的点、`AC-1` 的横线在字符类里有意义），
    所以一律 escape。三种写法覆盖实际见过的 PRD：

        - AC-1: 正文        列表项，冒号或全角冒号
        ### AC-1 正文       小标题
        AC-1. 正文          编号段落
    """
    e = re.escape(ref)
    return (
        # 列表项：-/*/+ 开头，编号后跟 : ： - – 或空格
        re.compile(rf"^[ \t]*[-*+][ \t]*{e}[ \t]*[:：\-–]?[ \t]*(?P<body>.*)$"),
        # 标题：# 开头
        re.compile(rf"^[ \t]*#{{1,6}}[ \t]*{e}[ \t]*[:：\-–]?[ \t]*(?P<body>.*)$"),
        # 裸编号起行：AC-1. / AC-1: / AC-1<空格>
        re.compile(rf"^[ \t]*{e}[ \t]*[.:：\-–][ \t]*(?P<body>.*)$"),
    )


def _continuation_lines(lines: list[str], start: int) -> list[str]:
    """一条标准的续行：缩进的、非空的、下一个编号/标题之前的行。

    多行标准是真实存在的（一条 AC 带几个子条件）。停在空行是刻意的：
    再往下贪就容易吞掉下一节，而吞错内容的代价是监工拿着别的标准判 diff。
    """
    out: list[str] = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if re.match(r"^[ \t]*(#{1,6}[ \t]|[-*+][ \t])", line):
            break
        if not re.match(r"^[ \t]+\S", line):
            break
        out.append(line.strip())
    return out


def find_in_text(text: str, ref: str) -> str | None:
    """在文档正文里找一个编号的正文。找不到返回 None。

    返回值**带编号**（`AC-1: 密码错误时返回 401`）：人在审计记录里要能对回
    原文档的哪一条，剥掉编号会让 claim 里的标准和 PRD 失去对应关系。

    只认第一次命中。文档里同一个编号出现两次是文档自己的问题，
    这里不做去重仲裁 —— 猜哪个是对的比抽不到更糟。
    """
    lines = text.splitlines()
    pats = _patterns(ref)
    for i, line in enumerate(lines):
        for pat in pats:
            m = pat.match(line)
            if not m:
                continue
            body = m.group("body").strip()
            parts = [body] if body else []
            parts.extend(_continuation_lines(lines, i))
            joined = " ".join(p for p in parts if p).strip()
            # 命中了行首但正文是空的 —— 文档里写了个编号没写内容。
            # 这和「找不到」是同一件事：没有可核对的东西。
            if not joined:
                return None
            return f"{ref}: {joined}"[:_MAX_BODY]
    return None


def resolve(refs, doc: str | Path | None, *, root: str | Path | None = None) -> Resolved:
    """把一串编号解析成正文。

    doc 为空但 refs 非空 → 直接算 doc_error。这是最常见的坏情况（原来的
    `examples/greet_task.yaml` 就是），必须有明确说法而不是静默放过。
    """
    wanted = tuple(str(r).strip() for r in (refs or ()) if str(r).strip())
    if not wanted:
        return Resolved()

    if not doc:
        return Resolved(doc_error="spec_ref 有编号但没有 spec_doc，编号无正文可查")

    path = Path(doc)
    if not path.is_absolute() and root is not None:
        path = Path(root) / path

    try:
        if not path.is_file():
            return Resolved(doc_error=f"spec_doc 不存在：{doc}")
        if path.stat().st_size > _MAX_DOC:
            return Resolved(doc_error=f"spec_doc 太大（>{_MAX_DOC} 字节）：{doc}")
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Resolved(doc_error=f"spec_doc 读不了：{doc}（{exc}）")

    found: list[tuple[str, str]] = []
    missing: list[str] = []
    for ref in wanted:
        body = find_in_text(text, ref)
        if body is None:
            missing.append(ref)
        else:
            found.append((ref, body))
    return Resolved(found=tuple(found), missing=tuple(missing))
