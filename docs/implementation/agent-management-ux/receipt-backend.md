# 后台接口交接回执 — 职能体元信息编辑

## 实际模型

claude-opus-4-6[1m]

## 提交

`b40258c` on `claude/agent-management-ux`（基线 `40dcf16`）

## 接口形状

```
PATCH /api/v4/agents/{aid}/metadata
Content-Type: application/json

{
  "name": "string | null",         // 1–120 字符，去首尾空白；空值拒绝
  "purpose": "string | null",      // 0–4000 字符，允许清空
  "expected_updated_at": "string"  // CAS 并发控制
}
// extra='forbid'，禁止额外字段
```

- 200 → 更新后的 agent JSON
- 400 → 空名 / 全空白名（ValueError 经 guarded 转 400）
- 403 → member 角色
- 404 → agent 不存在
- 409 → expected_updated_at 不匹配
- 422 → pydantic 校验失败（空字符串名 / 过长 / 额外字段）

## 改动文件

| 文件 | 变更 |
|------|------|
| `factory/control/agents.py` | AgentStore.__init__ 新增 `agent_metadata_audit` 审计表；新增 `update_metadata` 和 `metadata_audit` 方法 |
| `factory/control/agent_routes.py` | 新增 `MetadataUpdate` 模型和 `PATCH /agents/{aid}/metadata` 路由 |
| `tests/test_agent_metadata.py` | 12 条测试 |

## 测试与验证

### 命令

```bash
uv run --extra codex --extra claude pytest -q -p no:randomly tests/test_agent_metadata.py
# 退出码 0，12 passed

uv run --extra codex --extra claude pytest -q -p no:randomly -k "agent"
# 退出码 0，176 passed, 2528 deselected
```

### 8 条验收测试

| # | 测试 | 结果 |
|---|------|------|
| 1 | test_rename_saves_and_reads_back | PASS |
| 2 | test_empty_name_rejected + test_too_long_name_rejected | PASS |
| 3 | test_purpose_clearable | PASS |
| 4 | test_extra_fields_rejected_no_write | PASS |
| 5 | test_concurrent_modification_conflict | PASS |
| 6 | test_member_forbidden | PASS |
| 7 | test_rename_does_not_change_anything_else | PASS |
| 8 | test_audit_recorded | PASS |

### 3 条变异验证

| # | 变异 | 红 | 绿 |
|---|------|----|----|
| 4 | MetadataUpdate 改 extra="allow" | test_extra_fields_rejected_no_write FAILED, test_mutation_test4 FAILED | 恢复后 2 passed |
| 5 | 注释 expected_updated_at 检查 | test_concurrent_modification_conflict FAILED (200!=409), test_mutation_test5 FAILED | 恢复后 2 passed |
| 7 | update_metadata 偷改 active_version+1 | test_rename_does_not_change_anything_else FAILED (404), test_mutation_test7 FAILED | 恢复后 2 passed |

## 未改动（按实施单明确不碰）

- manifest.identity / Skill 正文 / 工具契约 / model_settings / active_version / 版本记录 / 历史产物
- PATCH_FIELDS 未扩大（name/purpose 不进 draft patch）
- 不做删除/归档/权限系统/新存储抽象
- 不触碰 frontend/ 下任何文件

## 未验证项

无。8 条测试 + 3 条变异全部执行并通过。
