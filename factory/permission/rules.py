"""权限门第一层：静态规则。不调模型，纯字符串/路径判断。

抄 handoff 的分级权限门，但只抄「静态规则 → 廉价模型 → 升人工」这个**分层
结构**，不抄它的人工审核回路（那违背无人值守）。

为什么需要这一层：现在 worker 跑的是 `--permission-mode acceptEdits`，
一路放行，全靠 bwrap 沙箱兜底。沙箱管的是**边界**（出不了 workspace），
管不了**边界内**的破坏 —— worker 在自己工作区里 `git reset --hard`、
删掉测试目录、把 .github/workflows 改成空操作，沙箱一个字都不会说。

三层的分工必须清楚，否则会变成「什么都问模型」（贵）或「什么都放过」（等于没门）：

  第 1 层 静态规则（本模块）  确定性、零成本、零延迟。能一眼判死的直接判。
  第 2 层 廉价模型审批者      判不出的交给 haiku。这层是让门能**自动**跑的关键。
  第 3 层 升 needs-human      模型也判不出、或审批者自己坏了。

**默认必须是 ALLOW，不是 DENY。** 这一条反直觉，但它决定这道门能不能上线：
worker 一次派发里有几百个工具调用，全部走模型审批的话成本翻几倍、延迟爆炸。
所以静态规则只拦「明确危险」的那一小撮，剩下的直接过。这道门防的是
「误伤级破坏」，不是「所有可疑操作」—— 后者是沙箱的活。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    #: 静态规则判不出，要往上一层（模型审批者）走。
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Ruling:
    decision: Decision
    #: 命中的规则名。审计里靠它统计「哪条规则在拦东西」，空串表示没命中规则。
    rule: str = ""
    reason: str = ""


#: 无论在哪都不许碰的路径模式（相对 workspace 根）。
#:
#: 这些是**闸门自己**：oracle_rules.yaml 定义 D 类硬闸门，.github/workflows
#: 是 CI 的判据来源，tests/ 是回归监工的依据。worker 能改它们就等于下次派发
#: 没有闸门 —— 而那种破坏在当轮的 diff 审查里看起来完全正常（"重构了测试"）。
#:
#: 沙箱拦不住这个：这些文件就在 workspace 里，worker 有合法写权限。
PROTECTED_PATHS: tuple[str, ...] = (
    ".github/workflows/**",
    "oracle_rules.yaml",
    "factory/permission/**",
    ".git/hooks/**",
)

#: 危险 shell 命令。匹配的是**命令语义**，不是子串——`git reset --hard` 危险，
#: 但 `echo "git reset --hard"` 不危险，所以用词边界锚定。
#:
#: 每条都要能说出「为什么沙箱拦不住」，否则不该在这里（重复防护是噪声）：
#:   git reset --hard / clean -fd  —— 在 workspace 内合法，但会抹掉未提交的产出，
#:                                    包括**上一轮监工审过的改动**
#:   git push --force              —— 走网络，沙箱没 --unshare-net
#:   rm -rf 带变量/通配            —— 展开结果不可静态预测
#:   chmod 777 / chown             —— 权限放大，为后续留后门
#:   curl|sh / wget|sh             —— 引入未审计代码，绕过整条流水线
_DANGEROUS: tuple[tuple[str, str, str], ...] = (
    (
        "git-reset-hard",
        r"\bgit\s+reset\s+(--hard|--merge)\b",
        "会抹掉未提交改动，包括上一轮监工已审过的产出",
    ),
    (
        "git-clean-force",
        r"\bgit\s+clean\s+-[a-z]*[fd]",
        "会删掉未跟踪文件，worker 自己刚生成的产出也在内",
    ),
    (
        "git-push-force",
        r"\bgit\s+push\b.*\s(--force|-f)\b",
        "改写远端历史，走网络，沙箱不拦（没有 --unshare-net）",
    ),
    (
        "rm-rf-unpredictable",
        r"\brm\s+-[a-z]*r[a-z]*f?\b.*[$*?]",
        "rm -rf 的目标含变量或通配，展开结果无法静态判断",
    ),
    (
        "chmod-world-writable",
        r"\bchmod\s+(-R\s+)?[0-7]?777\b",
        "把文件放成全局可写，是给后续派发留的后门",
    ),
    (
        "pipe-to-shell",
        r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b",
        "把网上的东西直接喂给 shell，引入的代码绕过了整条审计流水线",
    ),
    (
        "history-rewrite",
        r"\bgit\s+(filter-branch|filter-repo)\b|\bgit\s+rebase\b.*\s-i\b",
        "改写提交历史，审计里的 commit 指向会失效",
    ),
)

_COMPILED = tuple((name, re.compile(pat), why) for name, pat, why in _DANGEROUS)


def _match_path(path: str, pattern: str) -> bool:
    """glob 匹配，`**` 跨目录。

    用 PurePosixPath.match 不够：它的 `**` 在 3.11 上不跨目录。所以把模式
    转成正则，一次转换讲清楚语义，不依赖标准库版本间的差异。

    归一化只剥前导 `./`，**不能用 lstrip("./")** —— 那是按字符集剥，会把
    `.github/...` 开头那个点也吃掉，变成 `github/...`，于是
    `.github/workflows/**` 永远匹配不上。实测三条受保护路径全部漏放。
    这类 bug 的方向恰好是「放行」，也就是没人会来报的那一侧。
    """
    raw = path or ""
    while raw.startswith("./"):
        raw = raw[2:]
    p = str(PurePosixPath(raw))
    rx = re.escape(pattern).replace(r"\*\*", "\x00").replace(r"\*", "[^/]*")
    rx = rx.replace("\x00", ".*")
    return re.fullmatch(rx, p) is not None


def check_path(path: str) -> Ruling:
    """一个写入目标该不该放行。"""
    for pattern in PROTECTED_PATHS:
        if _match_path(path, pattern):
            return Ruling(
                Decision.DENY,
                rule=f"protected-path:{pattern}",
                reason=(
                    f"{path} 命中受保护路径 {pattern} —— 这些是闸门自己"
                    "（CI 判据、分级规则、权限门），改了等于下次派发没有闸门，"
                    "而当轮 diff 看起来完全正常"
                ),
            )
    return Ruling(Decision.ALLOW)


def check_command(command: str) -> Ruling:
    """一条 shell 命令该不该放行。

    判不出的返回 ESCALATE 而不是 ALLOW/DENY：命令的危险性常常取决于上下文
    （`rm -rf build/` 无害、`rm -rf $HOME` 灾难），静态规则只认明确的那些，
    含糊的交给第 2 层模型审批者。

    引号内的字符串不检查：`echo "git reset --hard"` 是无害的。只剥简单的
    成对引号（`"..."` 和 `'...'`），不做完整 shell 解析——那太重，而这层
    只需拦住「明确危险」的命令，不是当语法检查器。
    """
    text = (command or "").strip()
    if not text:
        return Ruling(Decision.ALLOW)
    
    # 剥掉简单的成对引号段。不递归、不处理转义、不管嵌套——只要能让
    # 常见的 echo/注释里的字面量不误报就够了。
    scrubbed = text
    for quote in ('"', "'"):
        parts = []
        in_quote = False
        start = 0
        for i, ch in enumerate(scrubbed):
            if ch == quote:
                if in_quote:
                    # 闭合：跳过这一段
                    start = i + 1
                    in_quote = False
                else:
                    # 开启：把前面未引用的部分留下
                    parts.append(scrubbed[start:i])
                    in_quote = True
        if not in_quote:
            parts.append(scrubbed[start:])
        scrubbed = " ".join(parts)
    
    for name, rx, why in _COMPILED:
        if rx.search(scrubbed):
            return Ruling(Decision.DENY, rule=name, reason=why)
    return Ruling(Decision.ALLOW)
