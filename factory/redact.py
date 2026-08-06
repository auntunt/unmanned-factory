"""落库前脱敏。

Global Constraint 11：任何密码 / API key 只按 key 名引用，不落库、不打印明文值。

监工的 claims 直接来自 check 命令的 stdout/stderr —— 一条
`curl -H "Authorization: Bearer $TOKEN"` 失败，token 就会进审计库。
所以脱敏放在 AuditStore 的写入边界上，而不是各个 supervisor 里：
和 D 类硬闸门同一个理由，不留旁路。
"""
from __future__ import annotations

import re

MASK = "***REDACTED***"

# 每条都保留 key 名，只吃掉值 —— 审计要能看出"这里有个密钥"，只是看不到它
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key=value / key: value / "key": "value"
    # 前缀 [A-Za-z0-9_.-]* 是为了吃掉 DEPLOY_PASSWORD 这种带前缀的名字：
    # \b 在 "DEPLOY_PASSWORD" 的 _P 处不成立，光靠 \b 会漏。
    re.compile(
        r"""(?i)([A-Za-z0-9_.\-]*
        (?:pass(?:wd|word)?|secret|token|api[_-]?key|access[_-]?key
        |secret[_-]?key|credential|auth[_-]?token|private[_-]?key)
        \s*["']?\s*[:=]\s*["']?)([^\s"',;)}\]]+)""",
        re.VERBOSE,
    ),
    # Authorization: Bearer xxx / Basic xxx
    re.compile(r"(?i)\b(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)(\S+)"),
    # sshpass -p xxx / mysql -pxxx / --password xxx
    re.compile(r"(?i)(\bsshpass\s+-p\s*)(\S+)"),
    re.compile(r"(?i)(--password[= ])(\S+)"),
    # 已知供应商前缀的裸 token（没有 key 名也要吃掉）
    re.compile(r"\b(sk-ant-)[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(sk-)[A-Za-z0-9]{20,}"),
    re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{16,}"),
    re.compile(r"\b(AKIA)[0-9A-Z]{12,}"),
    re.compile(r"\b(xox[baprs]-)[A-Za-z0-9-]{10,}"),
    # PEM 私钥整块
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def redact_text(text: str) -> str:
    """保留 group(1)（key 名或前缀），把其余部分换成掩码。

    无捕获组的模式（PEM 整块）整体替换。
    """
    out = text
    for pat in _PATTERNS:
        if pat.groups == 0:
            out = pat.sub(MASK, out)
        else:
            out = pat.sub(lambda m: m.group(1) + MASK, out)
    return out


def redact(value):
    """递归脱敏 str / dict / list / tuple，其他类型原样返回。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value
