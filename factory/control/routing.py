"""Server-side entry routing for the single chatbox.

One input, three real destinations, decided on the server (never trusting a
client-supplied provider/actor/agent permission):

  * ``development``  — a clear request to build or change software -> the existing
    project + run flow.
  * ``agent_chat``  — a daily task that a usable standalone role handles (quoting,
    meeting summaries, ...) -> that role's no-project ``do`` conversation. No
    project or run is created.
  * ``clarify``     — ambiguous or no matching role -> ask ONE necessary question
    and keep the original text. Ambiguity is never defaulted to coding.
  * ``unavailable`` — an explicitly chosen role that is missing/not ready/not
    permitted, or a capability whose real tool is not present yet (e.g. MFD
    conversion with no converter). Reported honestly; nothing is created.

Explicit role selection always wins over inference. Inference is a small,
transparent keyword rule set, deliberately conservative: when it is not clearly
one thing, it asks. This module is pure (no I/O) so it can be unit-tested; the
route endpoint wires it to the real agent list and actor.
"""
from __future__ import annotations

import re

# Signals that a request is really about building/changing software.
_DEV_TERMS = (
    "开发", "做一个", "做个", "写一个", "写个", "实现", "搭建", "重构", "部署", "上线",
    "网站", "网页", "小程序", "应用", "系统", "平台", "后端", "前端", "接口", "数据库",
    "爬虫", "脚本", "代码", "程序", "bug", "修复", "app", "api",
)
# Extra keywords that map to a role beyond its own name (name tokens are derived
# from the agent list at runtime; these cover common phrasings).
_ROLE_SYNONYMS = {
    "会议总结": ("会议", "纪要", "总结", "minutes"),
    "报价": ("报价", "算价", "造价单", "quote"),
}
_MFD_TERMS = ("mfd",)
_CJK = re.compile(r"[一-鿿]+")


def _has(text: str, terms) -> bool:
    low = text.lower()
    return any(t.lower() in low for t in terms)


def _name_tokens(name: str) -> list[str]:
    """Distinctive tokens for matching a role by name: the name minus common
    suffixes, plus any configured synonyms."""
    base = name.strip()
    for suffix in ("助手", "专家", "顾问", "小助手", "机器人"):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
    tokens = {base} if len(base) >= 2 else set()
    for key, syns in _ROLE_SYNONYMS.items():
        if key in name:
            tokens.update(syns)
    return [t for t in tokens if t]


def _is_mfd(agent, mfd_agent_id) -> bool:
    return (mfd_agent_id is not None and agent.get("id") == mfd_agent_id) or "mfd" in (agent.get("name", "").lower())


def _mfd_conversion_intent(text: str) -> bool:
    low = text.lower()
    if "mfd" in low:
        return True
    return ("造价" in text or "清单" in text or "定额" in text) and ("转换" in text or "xml" in low)


def _usable(agent, actor) -> bool:
    """A role is usable when it is ready (has an active version). Agents are shared
    org roles; per-user authorisation, when it exists, is enforced by the API layer.
    """
    if not agent.get("active_version"):
        return False
    return True


def _find(agents, agent_id):
    return next((a for a in agents if a.get("id") == agent_id), None)


def _label_for(agent):
    return agent.get("name") or "该角色"


def classify(text, agents, *, explicit_agent_id=None, actor=None,
             mfd_agent_id=None, mfd_executable=False) -> dict:
    """Return a routing decision dict. Pure: `agents` is the actor's available
    role list, already fetched and permission-scoped by the caller."""
    text = (text or "").strip()
    agents = list(agents or [])

    # --- explicit role selection wins over any inference -------------------
    if explicit_agent_id:
        agent = _find(agents, explicit_agent_id)
        if agent is None:
            return {"kind": "unavailable", "reason": "选择的角色不存在或你无权使用",
                    "mode_label": "暂不可用", "detail": "role_not_found"}
        if not _usable(agent, actor):
            return {"kind": "unavailable", "reason": f"「{_label_for(agent)}」尚未就绪，无法开始对话",
                    "mode_label": "暂不可用", "detail": "role_not_ready", "agent_id": agent["id"]}
        if _is_mfd(agent, mfd_agent_id) and not mfd_executable:
            return _mfd_unavailable(agent)
        return {"kind": "agent_chat", "agent_id": agent["id"], "agent_name": _label_for(agent),
                "reason": f"交给「{_label_for(agent)}」处理", "mode_label": f"「{_label_for(agent)}」",
                "seed": text}

    if not text:
        return {"kind": "clarify", "reason": "还没有内容", "mode_label": "需要你确认",
                "question": "想做什么？可以描述一个开发需求，或选择一个助手处理日常事务。",
                "roles": _role_options(agents)}

    # --- MFD conversion is honest about the missing converter --------------
    mfd_agent = next((a for a in agents if _is_mfd(a, mfd_agent_id)), None)
    if _mfd_conversion_intent(text):
        if mfd_agent is None:
            return {"kind": "unavailable", "mode_label": "暂不可用", "detail": "mfd_role_absent",
                    "reason": "MFD→XML 转换角色尚未提供，无法执行转换。"}
        if not mfd_executable:
            return _mfd_unavailable(mfd_agent)
        return {"kind": "agent_chat", "agent_id": mfd_agent["id"], "agent_name": _label_for(mfd_agent),
                "reason": f"交给「{_label_for(mfd_agent)}」处理", "mode_label": f"「{_label_for(mfd_agent)}」", "seed": text}

    # --- role match by name/synonym (skip dev-only and MFD roles) ----------
    matches = []
    for agent in agents:
        if mfd_agent is not None and agent.get("id") == mfd_agent.get("id"):
            continue
        if any(tok in text for tok in _name_tokens(agent.get("name", ""))):
            matches.append(agent)

    dev = _has(text, _DEV_TERMS)

    # Ambiguous: both a dev signal and a role match -> ask, never assume coding.
    if matches and dev:
        return {"kind": "clarify", "mode_label": "需要你确认",
                "reason": "既像开发，又像日常事务",
                "question": "这是要开发/修改软件，还是让某个助手处理？",
                "roles": _role_options(matches), "offer_development": True}
    if len(matches) == 1 and _usable(matches[0], actor):
        a = matches[0]
        return {"kind": "agent_chat", "agent_id": a["id"], "agent_name": _label_for(a),
                "reason": f"交给「{_label_for(a)}」处理", "mode_label": f"「{_label_for(a)}」", "seed": text}
    if len(matches) > 1:
        return {"kind": "clarify", "mode_label": "需要你确认", "reason": "匹配到多个角色",
                "question": "有多个助手可以处理，选一个？", "roles": _role_options(matches),
                "offer_development": dev}
    if dev:
        return {"kind": "development", "reason": "按开发处理", "mode_label": "开发"}

    # Neither clearly dev nor a known role: ask, do not default to coding.
    return {"kind": "clarify", "mode_label": "需要你确认", "reason": "无法确定处理方式",
            "question": "这是要开发/修改软件，还是让某个助手处理日常事务？",
            "roles": _role_options(agents), "offer_development": True}


def _role_options(agents):
    return [{"agent_id": a["id"], "name": _label_for(a)} for a in agents if _usable(a, None)][:12]


def _mfd_unavailable(agent):
    return {
        "kind": "unavailable", "mode_label": "暂不可用", "detail": "mfd_no_converter",
        "agent_id": agent.get("id"),
        "reason": "MFD→XML 转换器尚未接入：当前只有字段保真校验能力，没有可运行的转换器，无法把 MFD 转成 XML。",
        "missing": ["可运行的 MFD→XML 转换器源码", "对应 XSD 与可信参考 XML 的在线校验"],
    }
