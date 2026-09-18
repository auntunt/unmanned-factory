# V1 真实服务器核查（基线 7eb1749）

## 环境与启动方式

- Python: `.venv/bin/python`（项目虚拟环境）
- 启动方式: `uvicorn` 以子进程方式运行 `factory.control.app.create_app`，监听 `127.0.0.1:18799`
- 数据库: 临时目录下全新 `control.db`（SQLite），无既有数据
- 部署密钥目录: 独立临时目录（macOS 需 resolve 避 `/var` 符号链接）
- HTTP 客户端: `httpx.Client`，真实 TCP 连接，非 TestClient
- 模型替身: `DummyRunner`（返回固定 "OK"，不调用任何真实模型）
- 用户: `testadmin`（admin 角色）、`testmember`（member 角色），通过 `AuthStore.create_user` 在启动脚本中创建
- 重启测试: 真的 `proc.terminate()` + 重新 `Popen` + 等待端口可达

## 逐条结果

### 1. 运行环境角色裁剪 — 通过

**请求:**
```
Admin:  GET /api/v2/runtime → 200
Member: GET /api/v2/runtime → 200
```

**Admin 响应 keys:** `blockers, checked_at, configuration_revision, execution_mode, host, last_probes, limits, live_verified, local_readiness, profiles, readiness, tools, verification_note`

**Member 响应 keys:** `blockers, configuration_revision, local_readiness, readiness`

**Member 响应体:**
```json
{"readiness": {"planning": false, "execution": false, "publishing": false},
 "local_readiness": {"planning": true, "execution": true, "publishing": false},
 "blockers": ["GitHub publishing token is not present"],
 "configuration_revision": 1}
```

**逐 key 检查:** member 不含 `profiles`、`tools`、`host`、`auth_sources`、`limits`、`last_probes`、`execution_mode`、`live_verified`、`verification_note`、`checked_at`。member 含 `readiness`、`blockers`、`configuration_revision`。

**结论:** 敏感字段全部裁剪，必要字段完整。通过。

---

### 2. 配置写端点 — 通过

**请求与响应:**
```
Member PUT  /api/v2/runtime/profiles    → 403 {"detail":"此操作需要管理员权限"}
Member PUT  /api/v2/runtime/operations  → 403 {"detail":"此操作需要管理员权限"}
Member POST /api/v2/runtime/probe       → 403 {"detail":"此操作需要管理员权限"}
```

**结论:** 三个写端点对 member 全部 403。通过。

---

### 3. 会话 Skill 作用域 — 通过

**步骤与证据:**

1. 使用内置 agent `e88b9a59...`（需求分析），创建会话 A 和 B。
2. 向会话 A 导入 ZIP skill `test-skill`：
   ```
   POST /api/v4/sessions/{sid_a}/skills → 201
   ```
3. GET 会话 A skills → 1 条；GET 会话 B skills → 0 条。**作用域隔离正确。**
4. 检查 agent manifest `resolved_skills`：只有 `['遗留能力']`，无 `test-skill`。**未泄漏到团队能力库。**
5. DELETE 会话 A 的 skill → 200。再 GET → 0 条。**删除生效。**
6. 重新导入后尝试跨会话删除（用 B 的 session_id 删 A 的 skill_id）：
   ```
   DELETE /api/v4/sessions/{sid_b}/skills/{skill_id} → 404 {"detail":"绑定不存在"}
   ```
   **跨会话越权被阻止。**

**3b. 重启后恢复保留:**
- 导入 skill 后 `terminate` 进程，重新 `Popen` 同一数据库启动新进程
- 重启后 GET 会话 A skills → 2 条（含 restart-skill）
- **结论:** SQLite 持久化有效，重启后 skill 绑定保留。通过。

---

### 4. 固定测试地址 — 通过

**步骤与证据:**

1. Admin 创建 deploy target（`service_url: "https://app.example.com"`）：
   ```
   POST /api/v2/deploy-targets → 201
   ```
2. Admin 读取 target 完整配置：
   ```
   GET /api/v2/deploy-targets/{tid} → 200
   keys: commands, created_at, created_by, host, host_fingerprint, id, name,
         port, public_fingerprint, public_key, revision, service_url, user
   ```
   Admin 看到全部字段包括 `host`, `user`, `port`, `commands`, `host_fingerprint`。
3. 创建项目并绑定到 member，绑定 target 到项目。
4. Member 通过项目绑定查询：
   ```
   GET /api/v2/projects/{pid}/deploy-targets → 200
   available[0] keys: ['id', 'name', 'revision', 'service_url']
   available[0]: {"id":"...","name":"test-target","revision":1,"service_url":"https://app.example.com"}
   ```

**逐 key 检查:** member 只看到 `id`, `name`, `revision`, `service_url`。**不含** `host`, `user`, `port`, `commands`, `host_fingerprint`。

**结论:** 敏感连接信息完全隐藏，`service_url` 可见。通过。

---

### 5. 能力来源摘要 — 通过

**步骤与证据:**

1. 创建项目 + run（无任何执行记录）：
   ```
   POST /api/v2/runs → 201
   ```
2. 查询 deliverables：
   ```
   GET /api/v3/runs/{rid}/deliverables → 200
   ```
3. `capability_sources` 字段：
   ```json
   {
     "loaded":  {"status": "no_record", "items": []},
     "invoked": {"status": "no_record", "items": []}
   }
   ```

**结论:** 无记录时返回明确的 `status: "no_record"` 加空 `items` 列表。不是 null、不是空列表、不是编造内容。通过。

---

### 6. 配置工具暴露面 — 通过

**方法:** 直接调用 `create_server` 构造 MCP 服务器，通过 MCP 2.x `tools/list` handler 获取工具清单。非 HTTP 但走了真实的 `create_server` + MCP 协议往返。

**证据:**

1. **普通 do 模式会话**（admin 角色，`admin_config=False`）：
   ```
   tools: ['calc', 'export']
   ```
   只有 calc 和 export，无配置工具。

2. **管理员配置会话**（admin 角色，`admin_config=True`）：
   ```
   tools: ['calc', 'check_deploy_targets', 'configure_operations',
           'create_deploy_target', 'delete_deploy_target', 'export',
           'get_operations_config', 'get_runtime_config',
           'list_deploy_targets', 'update_deploy_target',
           'update_runtime_config']
   ```
   共 11 个工具，含 9 个配置工具。

3. **Member 角色 + `admin_config=True`**：
   ```
   tools: ['calc', 'export']
   ```
   即使 binding 声明 `admin_config=True`，member 角色仍只有 calc/export。配置工具被角色门控拦截。

**结论:** 双门控（角色 + admin_config 标志）均生效。普通会话只暴露 calc/export。通过。

## 发现的问题（按严重度排序）

无。全部 6 条（含 3b 重启测试）均通过。

## 未验证项与原因

- 需模型的路径（如 runtime probe 的真实 provider 调用）：使用 DummyRunner，未走真实模型推理。这在任务规格中已明确允许（"如果某条路径必须有模型才能走通，就不走那条"）。
- GitHub skill fetch（N4 的 GitHub 拉取路径）：需要 GitHub 网络访问，本次未测试。
