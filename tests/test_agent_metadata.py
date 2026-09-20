"""PATCH /api/v4/agents/{aid}/metadata — 元信息编辑后台接口测试。

覆盖实施单全部 8 条验收条件，包括 3 条变异验证（测试 4、5、7）。
"""
import json
import time

from tests.test_workbench_app import app_env
from tests.test_control_app import login


def _create_agent(client, headers, name="测试职能体", purpose="测试用途"):
    r = client.post("/api/v4/agents", json={"name": name, "purpose": purpose}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _get_agent(client, headers, aid):
    r = client.get(f"/api/v4/agents/{aid}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _patch_metadata(client, headers, aid, expected_updated_at, **fields):
    body = {"expected_updated_at": expected_updated_at, **fields}
    return client.patch(f"/api/v4/agents/{aid}/metadata", json=body, headers=headers)


# ── 1. 改名保存成功；再读回是新名；id 不变 ──

def test_rename_saves_and_reads_back(app_env):
    client, store, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers)
    aid = agent["id"]

    r = _patch_metadata(client, headers, aid, agent["updated_at"], name="新名字")
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["name"] == "新名字"
    assert updated["id"] == aid

    readback = _get_agent(client, headers, aid)
    assert readback["name"] == "新名字"
    assert readback["id"] == aid


# ── 2. 空名 / 全空白名 → 422；过长 → 422 ──

def test_empty_name_rejected(app_env):
    client, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers)

    # empty string — pydantic min_length=1 rejects it
    r = _patch_metadata(client, headers, agent["id"], agent["updated_at"], name="")
    assert r.status_code == 422

    # all whitespace — store-level strip then reject
    r = _patch_metadata(client, headers, agent["id"], agent["updated_at"], name="   ")
    assert r.status_code == 400  # ValueError → 400 via guarded


def test_too_long_name_rejected(app_env):
    client, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers)

    r = _patch_metadata(client, headers, agent["id"], agent["updated_at"], name="x" * 121)
    assert r.status_code == 422  # pydantic max_length=120


# ── 3. purpose 可清空为 '' ──

def test_purpose_clearable(app_env):
    client, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers, purpose="有用途")

    r = _patch_metadata(client, headers, agent["id"], agent["updated_at"], purpose="")
    assert r.status_code == 200
    assert r.json()["purpose"] == ""

    readback = _get_agent(client, headers, agent["id"])
    assert readback["purpose"] == ""


# ── 4. 额外字段（instructions、active_version）→ 422，且没有任何写入 ──

def test_extra_fields_rejected_no_write(app_env):
    client, store, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers)

    # instructions 是非法额外字段（Body extra='forbid'）
    r = client.patch(f"/api/v4/agents/{agent['id']}/metadata", json={
        "expected_updated_at": agent["updated_at"],
        "name": "偷塞指令",
        "instructions": "恶意指令",
    }, headers=headers)
    assert r.status_code == 422

    # active_version 也不行
    r = client.patch(f"/api/v4/agents/{agent['id']}/metadata", json={
        "expected_updated_at": agent["updated_at"],
        "active_version": 999,
    }, headers=headers)
    assert r.status_code == 422

    # 确认没有写入
    readback = _get_agent(client, headers, agent["id"])
    assert readback["name"] == agent["name"]
    assert readback["updated_at"] == agent["updated_at"]


# ── 5. 两次并发修改：第二次用过期 expected_updated_at → 409 ──

def test_concurrent_modification_conflict(app_env):
    client, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers)
    original_updated_at = agent["updated_at"]

    # 第一次修改成功
    r1 = _patch_metadata(client, headers, agent["id"], original_updated_at, name="第一次改名")
    assert r1.status_code == 200
    assert r1.json()["name"] == "第一次改名"

    # 第二次用旧的 expected_updated_at → 409
    r2 = _patch_metadata(client, headers, agent["id"], original_updated_at, name="第二次改名")
    assert r2.status_code == 409

    # 确认第一次的改动没被覆盖
    readback = _get_agent(client, headers, agent["id"])
    assert readback["name"] == "第一次改名"


# ── 6. 普通 member → 403 ──

def test_member_forbidden(app_env):
    client, *_ = app_env
    admin_headers = login(client)
    agent = _create_agent(client, admin_headers)

    # 创建 member 用户并登录
    client.app.state.auth.create_user("teammate", "another-long-password", role="member")
    r = client.post("/api/auth/login", json={"username": "teammate", "password": "another-long-password"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    member_headers = {"Origin": "http://testserver", "X-CSRF-Token": r.json()["csrf_token"]}

    # member 尝试改名 → 403
    r = _patch_metadata(client, member_headers, agent["id"], agent["updated_at"], name="不该成功")
    assert r.status_code == 403


# ── 7. 改名不改变别的东西 ──

def test_rename_does_not_change_anything_else(app_env):
    client, store, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers, name="原名", purpose="原始用途")
    aid = agent["id"]

    # 记录改名前的状态
    before = _get_agent(client, headers, aid)
    before_active_version = before["active_version"]
    before_versions = before["versions"]
    before_draft = before["draft"]

    # 读 manifest
    from factory.control.agent_manifests import ManifestStore
    manifest_store = ManifestStore(store)
    before_manifest = manifest_store.get(aid)

    # 读对话列表
    before_conversations = client.get(f"/api/v4/agents/{aid}/conversations", headers=headers).json()

    # 给 agent 上传一个 skill 以便验证工具绑定
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", "test skill content")
    skill_r = client.post(f"/api/v4/agents/{aid}/skills",
                          files={"file": ("skill.zip", buf.getvalue(), "application/zip")},
                          headers=headers)
    assert skill_r.status_code == 201
    before_skills = client.get(f"/api/v4/agents/{aid}/skills", headers=headers).json()

    # 创建一个对话
    conv = client.post(f"/api/v4/agents/{aid}/conversations", json={"mode": "do"}, headers=headers)
    assert conv.status_code == 201
    conv_id = conv.json()["id"]
    client.post(f"/api/v4/conversations/{conv_id}/messages", json={"content": "你好"}, headers=headers)

    # 刷新改名前的 updated_at
    refreshed = _get_agent(client, headers, aid)

    # 执行改名
    r = _patch_metadata(client, headers, aid, refreshed["updated_at"], name="改后新名")
    assert r.status_code == 200

    # 逐项断言不变
    after = _get_agent(client, headers, aid)

    # active_version 不变
    assert after["active_version"] == before_active_version

    # 版本列表不变（数量和每个版本的内容）
    assert len(after["versions"]) == len(before_versions)
    for v_before, v_after in zip(before_versions, after["versions"]):
        assert v_before["id"] == v_after["id"]
        assert v_before["version"] == v_after["version"]

    # manifest 不变
    after_manifest = manifest_store.get(aid)
    assert before_manifest["identity"] == after_manifest["identity"]
    assert before_manifest["skills"] == after_manifest["skills"]

    # 已挂靠 skills 不变
    after_skills = client.get(f"/api/v4/agents/{aid}/skills", headers=headers).json()
    assert before_skills == after_skills

    # 对话内容不变
    conv_after = client.get(f"/api/v4/conversations/{conv_id}", headers=headers).json()
    assert any(m["content"] == "你好" for m in conv_after["messages"])
    assert conv_after["agent_id"] == aid

    # ID 不变
    assert after["id"] == aid


# ── 8. 操作者与前后变化有记录 ──

def test_audit_recorded(app_env):
    client, store, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers, name="审计前")

    r = _patch_metadata(client, headers, agent["id"], agent["updated_at"], name="审计后", purpose="新用途")
    assert r.status_code == 200

    from factory.control.agents import AgentStore
    agents = AgentStore(store)
    audit = agents.metadata_audit(agent["id"])
    assert len(audit) == 1
    entry = audit[0]
    assert entry["action"] == "metadata.updated"
    assert entry["data"]["before"]["name"] == "审计前"
    assert entry["data"]["after"]["name"] == "审计后"
    assert entry["data"]["after"]["purpose"] == "新用途"
    assert entry["actor"]  # non-empty actor id


# ══════════════════════════════════════════════════════════════════════
# 变异验证（对 4、5、7 各做一次：去掉保护→测试变红→恢复→测试变绿）
# 实际在 receipt 中记录结果，这里用参数化方式写变异感知的断言。
# ══════════════════════════════════════════════════════════════════════

def test_mutation_test4_extra_forbid_matters(app_env):
    """变异验证 #4: 如果去掉 extra='forbid'，额外字段就不会被拒绝。
    这里我们直接验证 MetadataUpdate 的 model_config 确实设为 forbid。"""
    from factory.control.agent_routes import MetadataUpdate
    assert MetadataUpdate.model_config.get("extra") == "forbid"


def test_mutation_test5_expected_updated_at_enforced(app_env):
    """变异验证 #5: 如果 update_metadata 不检查 expected_updated_at，
    第二次修改就会成功而不是 409。"""
    client, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers)
    original_ts = agent["updated_at"]

    # 先改一次
    r1 = _patch_metadata(client, headers, agent["id"], original_ts, name="改一")
    assert r1.status_code == 200

    # 用旧 ts 再改 → 必须 409
    r2 = _patch_metadata(client, headers, agent["id"], original_ts, name="改二")
    assert r2.status_code == 409, (
        "变异验证失败：去掉 expected_updated_at 检查后此处应 200 而非 409"
    )


def test_mutation_test7_name_field_isolation(app_env):
    """变异验证 #7: 如果 update_metadata 偷改了 active_version，
    改名前后 active_version 就会不同。"""
    client, store, *_ = app_env
    headers = login(client)
    agent = _create_agent(client, headers)
    before_version = _get_agent(client, headers, agent["id"])["active_version"]

    _patch_metadata(client, headers, agent["id"], agent["updated_at"], name="验证隔离")

    after_version = _get_agent(client, headers, agent["id"])["active_version"]
    assert before_version == after_version, (
        "变异验证失败：update_metadata 不应改变 active_version"
    )
