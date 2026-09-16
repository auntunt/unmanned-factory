"""Server-side entry routing: one chatbox input -> development / agent_chat /
clarify / unavailable, decided on the server. Explicit role selection wins over
inference; ambiguity never defaults to coding; the MFD role is honest about the
missing converter; normal routing and chat create no project/run.
"""
import uuid

from factory.control import routing
from tests.test_control_app import login
from tests.test_workbench_app import app_env  # noqa: F401

MFD_ID = uuid.uuid5(uuid.NAMESPACE_URL, "webuddy:agent-pack:mfd-xml-conversion").hex


def _agent(name, aid=None, active=1):
    return {"id": aid or uuid.uuid4().hex, "name": name, "active_version": active}


QUOTE = _agent("报价助手")
SUMMARY = _agent("会议总结助手")
MFD = _agent("MFD→XML 造价数据转换", MFD_ID)
ROLES = [QUOTE, SUMMARY, MFD]


# --------------------------------------------------------------- pure classifier

def test_clear_development_request_routes_to_development():
    d = routing.classify("做一个能预约、改期和导出记录的管理工具", ROLES, mfd_agent_id=MFD_ID)
    assert d["kind"] == "development"


def test_daily_task_routes_to_matching_role_no_run():
    d = routing.classify("客户要一批设备的报价，单价乘数量再打九折", ROLES, mfd_agent_id=MFD_ID)
    assert d["kind"] == "agent_chat" and d["agent_id"] == QUOTE["id"]
    d2 = routing.classify("帮我把这次会议纪要整理成要点", ROLES, mfd_agent_id=MFD_ID)
    assert d2["kind"] == "agent_chat" and d2["agent_id"] == SUMMARY["id"]


def test_explicit_role_selection_overrides_inference():
    # Text looks like development, but the user explicitly picked the quote role.
    d = routing.classify("做一个系统", ROLES, explicit_agent_id=QUOTE["id"], mfd_agent_id=MFD_ID)
    assert d["kind"] == "agent_chat" and d["agent_id"] == QUOTE["id"]


def test_ambiguous_input_asks_and_never_defaults_to_coding():
    # Contains a dev term (做个) and a role hit (报价) -> must clarify, not code.
    d = routing.classify("做个报价", ROLES, mfd_agent_id=MFD_ID)
    assert d["kind"] == "clarify"
    # Fully unknown -> clarify, still not development.
    d2 = routing.classify("你好", ROLES, mfd_agent_id=MFD_ID)
    assert d2["kind"] == "clarify"


def test_mfd_conversion_is_unavailable_without_converter():
    for text in ("把这个 mfd 转成 xml", "帮我做造价文件的 XML 转换"):
        d = routing.classify(text, ROLES, mfd_agent_id=MFD_ID, mfd_executable=False)
        assert d["kind"] == "unavailable" and d["detail"] == "mfd_no_converter"
        assert d["missing"]  # honest missing-item list, no task created


def test_explicit_mfd_role_is_also_unavailable():
    d = routing.classify("转换一下", ROLES, explicit_agent_id=MFD_ID, mfd_agent_id=MFD_ID, mfd_executable=False)
    assert d["kind"] == "unavailable" and d["detail"] == "mfd_no_converter"


def test_explicit_unknown_or_unready_role_is_unavailable():
    missing = routing.classify("算个价", ROLES, explicit_agent_id="nope", mfd_agent_id=MFD_ID)
    assert missing["kind"] == "unavailable" and missing["detail"] == "role_not_found"
    unready = routing.classify("算个价", [_agent("草稿角色", active=0)],
                               explicit_agent_id=None, mfd_agent_id=MFD_ID)
    # an unready role is not matched by inference either -> clarify, not agent_chat
    assert unready["kind"] == "clarify"


# ------------------------------------------------------------------- endpoint

def test_route_endpoint_decides_server_side_and_creates_nothing(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    # ambiguous / daily / dev all go through the same endpoint; none creates a run.
    r = client.post("/api/v4/route", json={"text": "做一个预约管理工具"}, headers=headers)
    assert r.status_code == 200 and r.json()["kind"] == "development"
    r2 = client.post("/api/v4/route", json={"text": "把这个 mfd 转成 xml"}, headers=headers)
    assert r2.json()["kind"] == "unavailable" and r2.json()["detail"] == "mfd_no_converter"
    assert store.runs() == []  # routing never creates a run or project


def test_route_to_agent_chat_then_conversation_has_no_run(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    agent = client.post("/api/v4/agents", json={"name": "报价助手", "purpose": "报价"}, headers=headers).json()
    decided = client.post("/api/v4/route", json={"text": "客户要设备报价，帮我算一下"}, headers=headers).json()
    assert decided["kind"] == "agent_chat" and decided["agent_id"] == agent["id"]
    # Acting on the decision creates a do conversation, not a project/run.
    conv = client.post(f"/api/v4/agents/{agent['id']}/conversations",
                       json={"mode": "do", "project_id": None}, headers=headers).json()
    assert conv["mode"] == "do" and conv.get("project_id") in (None, "")
    assert store.runs() == []
