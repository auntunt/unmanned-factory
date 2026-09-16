"""Server-side entry routing for the single chatbox.

One input (text plus optional attachment metadata), four real destinations, decided
on the server — never trusting a client-supplied provider/actor/agent permission:

  * ``development``  — an explicit request to build or change software (a build verb
    AND a software object) -> the existing project + run flow. Only an explicit
    development request imports files as a project.
  * ``agent_chat``  — a daily task a usable standalone role handles (quoting, meeting
    summaries, ...) -> that role's no-project ``do`` conversation. No project/run.
    UTF-8 text attachments (.txt/.md/.csv) ride along as conversation attachments.
  * ``clarify``     — ambiguous, a daily need with no available role, or material
    whose purpose is unclear -> ask ONE necessary question and keep the original
    text and material. Ambiguity is never resolved to coding; a technical question
    is not a development request; a missing role is asked/flagged, not coded around.
  * ``unavailable`` — an explicitly chosen role that is missing/not ready/not
    chat-capable, a capability whose real tool is absent (MFD conversion with no
    converter, incl. any .mfd attachment), or an attachment format/size a daily
    chat cannot take. Reported honestly; nothing is created.

Explicit role selection wins over inference. A role is only routable when it is
ready AND its effective provider actually supports the session chat tools (the
caller annotates ``chat_capable`` from the server-side config; a role whose
provider rejects the tools is never auto-routed into a failing chat).

This module is pure (no I/O) so it can be unit-tested; the route endpoint wires it
to the real agent list, the effective provider config, and the server-side actor.
"""
from __future__ import annotations

# A development request needs BOTH a build verb and a software object; a lone verb
# ("写一个", "做一个", "修复") or a lone object ("API") is not enough.
_BUILD_VERBS = (
    "开发", "做一个", "做个", "做", "写一个", "写个", "写", "编写", "搭建", "搭", "实现",
    "生成", "构建", "重构", "部署", "上线", "集成", "对接", "调试", "修复", "修改",
)
_SOFTWARE_OBJECTS = (
    "系统", "网站", "网页", "应用", "小程序", "工具", "程序", "软件", "接口", "服务",
    "平台", "数据库", "脚本", "爬虫", "插件", "页面", "前端", "后端", "功能", "模块",
    "组件", "表单", "仪表盘", "后台", "管理系统", "网关", "转换器", "app", "api", "sdk",
    "cli", "bug", "ui", "h5", "网页版",
)
# Daily-task intents, independent of which roles happen to exist. Used both to find
# a role and, when none exists, to ask for that specific role instead of coding.
_DAILY_INTENTS = {
    "报价": ("报价", "算价", "造价单", "报价单"),
    "会议总结": ("会议纪要", "会议总结", "会议记录", "纪要", "minutes"),
}
_MFD_TEXT = ("mfd",)
_TEXT_EXT = (".txt", ".md", ".csv")
_TEXT_ATTACH_LIMIT = 40_000  # matches the conversation attachment endpoint


def _has(text, terms):
    low = text.lower()
    return any(t.lower() in low for t in terms)


def _is_dev(text: str) -> bool:
    return _has(text, _BUILD_VERBS) and _has(text, _SOFTWARE_OBJECTS)


def _name_tokens(name: str):
    base = name.strip()
    for suffix in ("助手", "专家", "顾问", "小助手", "机器人"):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
    return [base] if len(base) >= 2 else []


def _is_mfd(agent, mfd_agent_id) -> bool:
    return (mfd_agent_id is not None and agent.get("id") == mfd_agent_id) or "mfd" in (agent.get("name", "").lower())


def _mfd_text_intent(text: str) -> bool:
    low = text.lower()
    if "mfd" in low:
        return True
    return ("造价" in text or "清单" in text or "定额" in text) and ("转换" in text or "xml" in low)


def _att_kind(att) -> str:
    name = str(att.get("name", "")).lower()
    if name.endswith(".mfd"):
        return "mfd"
    if name.endswith(_TEXT_EXT):
        size = att.get("size")
        if isinstance(size, (int, float)) and size > _TEXT_ATTACH_LIMIT:
            return "toobig"
        return "text"
    return "other"


def _ready(agent) -> bool:
    return bool(agent.get("active_version"))


def _capable(agent) -> bool:
    # A role is routable to chat only when ready AND its provider supports the
    # session tools. `chat_capable` is annotated by the endpoint from the effective
    # server-side config; absence means "assume capable" only in pure unit tests.
    return _ready(agent) and agent.get("chat_capable", True)


def _label(agent):
    return agent.get("name") or "该角色"


def _role_options(agents):
    return [{"agent_id": a["id"], "name": _label(a)} for a in agents if _capable(a)][:12]


def _mfd_unavailable(agent):
    return {"kind": "unavailable", "mode_label": "暂不可用", "detail": "mfd_no_converter",
            "agent_id": (agent or {}).get("id"),
            "reason": "MFD→XML 转换器尚未接入：当前只有字段保真校验能力，没有可运行的转换器，无法把 MFD 转成 XML。",
            "missing": ["可运行的 MFD→XML 转换器源码", "对应 XSD 与可信参考 XML 的在线校验"]}


def _format_unavailable(names):
    return {"kind": "unavailable", "mode_label": "暂不可用", "detail": "attachment_unsupported",
            "reason": "日常助手对话只支持 UTF-8 文本附件（.txt / .md / .csv，单个 ≤40KB）。",
            "unsupported": names,
            "missing": ["把材料转成 UTF-8 文本，或改用明确的开发任务导入项目"]}


def classify(text, agents, *, explicit_agent_id=None, actor=None,
             mfd_agent_id=None, mfd_executable=False, attachments=None) -> dict:
    """Return a routing decision dict. Pure: `agents` is the actor's available,
    permission-scoped role list, each optionally annotated with `chat_capable`."""
    text = (text or "").strip()
    agents = list(agents or [])
    atts = list(attachments or [])
    kinds = [_att_kind(a) for a in atts]
    has_mfd_att = "mfd" in kinds
    bad_atts = [a.get("name") for a, k in zip(atts, kinds) if k in ("toobig", "other")]
    supported_text_atts = bool(atts) and all(k == "text" for k in kinds)
    dev_intent = _is_dev(text)
    mfd_agent = next((a for a in agents if _is_mfd(a, mfd_agent_id)), None)

    # --- explicit role selection wins over inference -----------------------
    if explicit_agent_id:
        agent = next((a for a in agents if a.get("id") == explicit_agent_id), None)
        if agent is None:
            return {"kind": "unavailable", "mode_label": "暂不可用", "detail": "role_not_found",
                    "reason": "选择的角色不存在或你无权使用"}
        if _is_mfd(agent, mfd_agent_id) and not mfd_executable:
            return _mfd_unavailable(agent)
        if not _ready(agent):
            return {"kind": "unavailable", "mode_label": "暂不可用", "detail": "role_not_ready",
                    "reason": f"「{_label(agent)}」尚未就绪，无法开始对话", "agent_id": agent["id"]}
        if not agent.get("chat_capable", True):
            return {"kind": "unavailable", "mode_label": "暂不可用", "detail": "provider_no_chat_tools",
                    "reason": f"「{_label(agent)}」当前的模型配置不支持对话工具（计算/导出），无法用于日常对话。",
                    "agent_id": agent["id"]}
        if atts and not supported_text_atts:
            return _mfd_unavailable(mfd_agent) if has_mfd_att else _format_unavailable(bad_atts)
        return {"kind": "agent_chat", "agent_id": agent["id"], "agent_name": _label(agent),
                "reason": f"交给「{_label(agent)}」处理", "mode_label": f"「{_label(agent)}」",
                "seed": text, "attach": supported_text_atts}

    if not text and not atts:
        return {"kind": "clarify", "mode_label": "需要你确认", "reason": "还没有内容",
                "question": "想做什么？可以描述一个开发需求，或让某个助手处理日常事务。",
                "roles": _role_options(agents)}

    # --- MFD is honest about the missing converter (text intent or .mfd file) --
    if (mfd_att := has_mfd_att) or _mfd_text_intent(text):
        if dev_intent and not mfd_att:
            pass  # "开发一个 MFD 转换器" is a real build request; fall through to development
        elif mfd_agent is None:
            return {"kind": "unavailable", "mode_label": "暂不可用", "detail": "mfd_role_absent",
                    "reason": "MFD→XML 转换角色尚未提供，无法执行转换。"}
        else:
            return _mfd_unavailable(mfd_agent)

    # --- explicit development: build verb + software object ------------------
    if dev_intent:
        return {"kind": "development", "reason": "按开发处理", "mode_label": "开发"}

    # --- daily intent / role match (skip the MFD role) ----------------------
    daily_hit = any(_has(text, syns) for syns in _DAILY_INTENTS.values())
    matched = []
    for agent in agents:
        if mfd_agent is not None and agent.get("id") == mfd_agent.get("id"):
            continue
        by_intent = any(any(s in agent.get("name", "") for s in syns)
                        for key, syns in _DAILY_INTENTS.items() if _has(text, syns))
        by_name = any(tok in text for tok in _name_tokens(agent.get("name", "")))
        if by_intent or by_name:
            matched.append(agent)
    capable = [a for a in matched if _capable(a)]

    if len(capable) == 1:
        a = capable[0]
        if atts and not supported_text_atts:
            return _format_unavailable(bad_atts)
        return {"kind": "agent_chat", "agent_id": a["id"], "agent_name": _label(a),
                "reason": f"交给「{_label(a)}」处理", "mode_label": f"「{_label(a)}」",
                "seed": text, "attach": supported_text_atts}
    if len(capable) > 1:
        return {"kind": "clarify", "mode_label": "需要你确认", "reason": "匹配到多个角色",
                "question": "有多个助手可以处理，选一个？", "roles": _role_options(capable)}
    if daily_hit or matched:
        # A daily need with no usable role: ask/flag the missing role, never fall to coding.
        return {"kind": "clarify", "mode_label": "需要你确认", "detail": "role_missing",
                "reason": "看起来是日常事务，但没有可用且支持对话的对应角色",
                "question": "需要一个能处理这件事的助手，但当前没有可用的。先创建/启用对应角色，或改成明确的开发任务？",
                "roles": _role_options(agents)}

    # --- attachments whose purpose is unclear: keep them, ask ----------------
    if atts:
        if has_mfd_att:
            return _mfd_unavailable(mfd_agent)
        return {"kind": "clarify", "mode_label": "需要你确认", "detail": "material_purpose",
                "reason": "收到材料，但不确定用途",
                "question": "这些材料是要用来开发，还是交给某个助手处理？",
                "roles": _role_options(agents), "offer_development": True,
                "unsupported": bad_atts}

    # Neither clearly development nor a known daily role: ask, never assume code.
    return {"kind": "clarify", "mode_label": "需要你确认", "reason": "无法确定处理方式",
            "question": "这是要开发/修改软件，还是让某个助手处理日常事务？",
            "roles": _role_options(agents), "offer_development": True}
