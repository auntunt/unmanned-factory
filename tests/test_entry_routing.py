"""Server-side entry routing: one chatbox input (text + attachment metadata) ->
development / agent_chat / clarify / unavailable, decided on the server.

Covers: explicit role wins; ambiguity never defaults to coding; weak dev tokens
are not development; a daily need with no usable role clarifies (not code); MFD
text/attachment is not executable; UTF-8 text attachments ride a role chat while
unsupported/.mfd attachments do not; a role whose effective provider can't run the
chat tools is not auto-routed; a member is really 403'd; nothing creates project/run.
"""
import uuid

from factory.control import routing
from tests.test_control_app import login
from tests.test_workbench_app import app_env  # noqa: F401

MFD_ID = uuid.uuid5(uuid.NAMESPACE_URL, "webuddy:agent-pack:mfd-xml-conversion").hex


def _agent(name, aid=None, active=1, capable=True):
    return {"id": aid or uuid.uuid4().hex, "name": name, "active_version": active, "chat_capable": capable}


QUOTE = _agent("报价助手")
SUMMARY = _agent("会议总结助手")
MFD = _agent("MFD→XML 造价数据转换", MFD_ID)
ROLES = [QUOTE, SUMMARY, MFD]


def _k(text, **kw):
    kw.setdefault("mfd_agent_id", MFD_ID)
    return routing.classify(text, kw.pop("agents", ROLES), **kw)["kind"]


# ----------------------------------------------- weak dev tokens are not dev

def test_weak_verb_or_lone_object_is_not_development():
    # Codex's real sentences: none is a software-build request.
    assert routing.classify("写一个会议总结", ROLES, mfd_agent_id=MFD_ID)["agent_id"] == SUMMARY["id"]
    assert routing.classify("做一个报价单", ROLES, mfd_agent_id=MFD_ID)["agent_id"] == QUOTE["id"]
    assert _k("解释一下API是什么") == "clarify"          # technical Q&A, not development
    assert routing.classify("帮我修复这段会议纪要错别字", ROLES, mfd_agent_id=MFD_ID)["agent_id"] == SUMMARY["id"]


def test_missing_role_daily_need_clarifies_not_codes():
    # Same daily sentences, but no roles exist -> clarify (ask/flag), never development.
    for text in ("写一个会议总结", "做一个报价单", "帮我修复这段会议纪要错别字"):
        d = routing.classify(text, [], mfd_agent_id=MFD_ID)
        assert d["kind"] == "clarify" and d.get("detail") == "role_missing"


def test_real_build_request_is_development():
    assert _k("做一个能预约、改期和导出记录的管理工具") == "development"
    assert _k("写一个爬虫脚本把网页数据抓下来") == "development"
    assert _k("帮我修复登录接口的 bug") == "development"


# ----------------------------------------------- explicit selection & ambiguity

def test_explicit_role_selection_overrides_inference():
    d = routing.classify("做一个系统", ROLES, explicit_agent_id=QUOTE["id"], mfd_agent_id=MFD_ID)
    assert d["kind"] == "agent_chat" and d["agent_id"] == QUOTE["id"]


def test_ambiguous_build_and_role_clarifies():
    # "做个报价工具" = build a quoting tool (dev object 工具) vs use the quote role.
    d = routing.classify("做个报价工具", ROLES, mfd_agent_id=MFD_ID)
    assert d["kind"] == "development" or d["kind"] == "clarify"  # never silently a role chat
    assert _k("你好") == "clarify"


# ------------------------------------------------------------- MFD honesty

def test_mfd_text_or_attachment_is_unavailable_without_converter():
    assert routing.classify("把这个 mfd 转成 xml", ROLES, mfd_agent_id=MFD_ID)["detail"] == "mfd_no_converter"
    d = routing.classify("处理一下这个文件", ROLES, mfd_agent_id=MFD_ID,
                         attachments=[{"name": "2.mfd", "size": 58528}])
    assert d["kind"] == "unavailable" and d["detail"] == "mfd_no_converter"


def test_explicit_mfd_build_request_is_development():
    # "开发一个 MFD 转换器" is a genuine software build, not the unavailable capability.
    assert _k("开发一个 MFD 转换器程序") == "development"


# ------------------------------------------------------------- attachments

def test_text_attachment_rides_a_role_chat():
    d = routing.classify("按这个价格表报价", ROLES, mfd_agent_id=MFD_ID,
                         attachments=[{"name": "prices.csv", "size": 2000}])
    assert d["kind"] == "agent_chat" and d["agent_id"] == QUOTE["id"] and d["attach"] is True


def test_unsupported_attachment_on_daily_is_format_unavailable():
    d = routing.classify("按这个报价", ROLES, mfd_agent_id=MFD_ID,
                         attachments=[{"name": "sheet.xlsx", "size": 5000}])
    assert d["kind"] == "unavailable" and d["detail"] == "attachment_unsupported"


def test_oversized_text_attachment_is_unsupported():
    d = routing.classify("报价", ROLES, mfd_agent_id=MFD_ID,
                         attachments=[{"name": "big.csv", "size": 500000}])
    assert d["kind"] == "unavailable" and d["detail"] == "attachment_unsupported"


def test_material_without_clear_purpose_clarifies_and_keeps_it():
    d = routing.classify("看看这个", ROLES, mfd_agent_id=MFD_ID,
                         attachments=[{"name": "notes.pdf", "size": 3000}])
    assert d["kind"] == "clarify" and d["detail"] == "material_purpose" and d["offer_development"]


# ------------------------------------------------- provider capability

def test_role_whose_provider_cannot_run_tools_is_not_auto_routed():
    codex_quote = _agent("报价助手", capable=False)
    d = routing.classify("帮我算个报价", [codex_quote], mfd_agent_id=MFD_ID)
    assert d["kind"] == "clarify" and d["detail"] == "role_missing"  # no capable role -> ask, not fail
    explicit = routing.classify("报价", [codex_quote], explicit_agent_id=codex_quote["id"], mfd_agent_id=MFD_ID)
    assert explicit["kind"] == "unavailable" and explicit["detail"] == "provider_no_chat_tools"


# ------------------------------------------------------------- endpoint

def test_route_endpoint_creates_nothing_and_is_server_side(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    assert client.post("/api/v4/route", json={"text": "做一个预约管理工具"}, headers=headers).json()["kind"] == "development"
    mfd = client.post("/api/v4/route", json={"text": "把 mfd 转成 xml"}, headers=headers).json()
    assert mfd["kind"] == "unavailable" and mfd["detail"] == "mfd_no_converter"
    assert store.runs() == []


def test_endpoint_capability_from_effective_config_then_agent_chat(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    agent = client.post("/api/v4/agents", json={"name": "报价助手", "purpose": "报价"}, headers=headers).json()
    # Default profiles are codex -> the role is not chat-capable -> daily need clarifies.
    codex = client.post("/api/v4/route", json={"text": "帮客户算一批设备的报价"}, headers=headers).json()
    assert codex["kind"] == "clarify" and codex["detail"] == "role_missing"
    # Make the planner profile claude -> the same role becomes chat-capable.
    cfg = service.runtime_settings.get()
    service.runtime_settings.update({"profiles": {**cfg["profiles"], "planner": {"provider": "claude", "model": "test"}},
                                    "limits": cfg["limits"]}, cfg["revision"], "tester")
    ok = client.post("/api/v4/route", json={"text": "帮客户算一批设备的报价"}, headers=headers).json()
    assert ok["kind"] == "agent_chat" and ok["agent_id"] == agent["id"]
    assert store.runs() == []


def test_member_is_forbidden_from_routing(app_env):
    client, store, service, repo = app_env
    client.app.state.auth.create_user("teammate", "another-long-password", role="member")
    r = client.post("/api/auth/login", json={"username": "teammate", "password": "another-long-password"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    member = {"Origin": "http://testserver", "X-CSRF-Token": r.json()["csrf_token"]}
    blocked = client.post("/api/v4/route", json={"text": "做一个报价单"}, headers=member)
    assert blocked.status_code == 403
