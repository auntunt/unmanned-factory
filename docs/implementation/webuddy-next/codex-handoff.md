# webuddy-next → Codex 验收交接（2026-09-19）

集成分支 `codex/autonomous-factory-v3`（本地 `v3-conversation-workspace`）。
**基线 `6fe1883` → 候选 `16dfdaa`**，50 个提交，10 个任务合并。生产仍是 `4c56632`，本轮未部署。
所有任务 local_reviewed / ready_for_codex；accepted 由你判定。

## 一、本轮做了什么（按六个结果维度）

开工先做了六份只读维度调查（`survey/D1.md`–`D6.md`），结论是**只有四个维度有真实代码缺口**：

| 维度 | 结论 | 任务 |
|---|---|---|
| D5 干预与恢复 | **无关键缺失**，四条期望全覆盖，未派任务 | — |
| D1 成果展示 | 上一轮登记的 `delivery_type` 前端缺口**已不成立**；真实缺口是能力来源摘要 | N6、N7 |
| D2 Skill/工具 | **最大缺口**：会话级绑定完全不存在 | N3、N4、N5 |
| D3 工具独立使用 | T04 已验证部分不重做；缺 CLI/版本/下载核对 | N9 |
| D4 配置归管理员 | 缺角色裁剪、member 可用性视图、配置对话 | N1、N2、N11 |
| D6 交付路径 | GitHub/SSH 部署已实现无 mock；唯一缺可展示地址字段 | N8 |

### 合并顺序与内容
- `27d3b25` N1：`GET /api/v2/runtime` 按角色裁剪（member 只得 readiness/blockers/configuration_revision）；`PUT runtime/profiles`、`PUT runtime/operations`、`POST runtime/probe` 补路由层显式 admin 检查；新增 member 运行状态视图 `RuntimeReadiness.tsx`。
- `44f124e` N8：`deploy_targets` 增可选 `service_url`（仅 http/https，非法值结构化拒绝）；member 经项目绑定查询只见 `id/name/revision/service_url`。
- `8bcf5c3` N9：工具 CLI harness + `VERSION` + `--version`；工具执行包下载 + sha256 核对；**MFD `pack.json` purpose 由「无损转换」改为「局部提取」**，与工具层 7 处口径对齐。
- `13e823e` N3（契约单）：新表 `session_skills(id, session_id, data, created_at)` + `POST/GET/DELETE /api/v4/sessions/{sid}/skills`；`session_skill_snapshot` 与既有 `module_snapshot` 并列注入 run，不改后者语义。session_id = `agent_conversations.id`。
- `ae0e87c` N2：9 个管理员配置对话工具，内部调用与表单**同一批**后端方法（`RuntimeSettings.update` / `OperationsAutomation.configure` / `TargetStore` CRUD），不新增 HTTP 配置端点。
- `8793f62` N7：非 general 操作（bugfix/release/startup/dependencies）在 `run_routes.new_run()` 接入既有 `infer_delivery_type_from_text`；**推导规则未改**，歧义不写、保持「未声明」。
- `307ea58` N5：会话 Skill 面板 + 术语呈现对齐；`AgentManifest` 由「skill 是工具」改为「skill 是方法与知识」。
- `0909ff7` N4：`origin="github"` 来源导入，`source_version` 记解析后的 40 位 commit sha（非分支名），与 ZIP 共用解析/校验路径。
- `800b4eb` N6：能力来源聚合 `capability_sources: {loaded:{status,items}, invoked:{status,items}}` + `service_urls`；loaded 用 `origin` 区分 `project_module` / `session_skill`；无记录返回 `status:"no_record"`。
- `81e8213` N11：管理员配置对话入口 `POST/GET /api/v4/admin-config/conversations`，组件挂在 `/settings/runtime` 与表单并列。

## 二、集成时发现并修掉的问题（子代理自测没抓到的）

这四条都是集成或全量阶段才暴露的，请重点复核：

1. **`7eb1749` 配置工具暴露面（最严重）**。N2 把 9 个配置工具注册给**所有会话**，意味着管理员在一个「报价助手」聊天里，模型手上也带着能改部署目标的工具。N2 自己 27 条测试全绿，因为它只测了「member 调用被拒绝」，没测「不该出现在清单里」。是既有 `test_real_session_mcp_server_calc_and_export_handlers` 在**全量**跑里变红才暴露。
   修法：双条件闸门——`actor_role=='admin'` **且** binding 显式 `admin_config=True`；标志位只能服务端按真实角色给。新增 `tests/test_admin_config_surface_gate.py` 5 条 + 变异验证（去掉 `and tools.admin_config` 后「管理员普通聊天不暴露配置工具」变红）。
   N11 随后建了合法入口把标志位置上，否则这个功能会一直不可达——**没有为了让功能看起来完整而放宽闸门**。
2. **N5 组件两处未防御的读**。`setSkills(data.items)` 和 `deps.length`，响应缺字段时整个会话页白屏；合并后 6 条既有测试变红暴露。已加防御 + 2 条回归测试。
3. **N5 的挂载点判断错误**。回执称挂在 `RunWorkspace`，但那里只有 `runId`，没有 `agent_conversations.id`。实际挂到 `AgentChatPage` 的 `cv-dock`（与既有 `CapabilityPanel` 并列）。
4. **N5 只跑 `tsc --noEmit` 没跑 `npm run build`**，build 的 `tsc -b` 更严，抓到未用参数。已修并在后续任务单里写明两个都要跑。

另外补了 N11 漏掉的 `AdminConfigChat` 前端测试 4 条。

## 三、测试证据（本机 macOS，集成工作区，`-p no:randomly`）

- **后端全量（最终树 `16dfdaa`）：`uv run pytest tests -m "not smoke" -q` → 2582 passed / 0 failed / 20 skipped / 2 deselected，退出码 0。**
- 中间两次全量：`7eb1749` 修闸门前 2569 passed / **1 failed**（即上面第 1 条）；修后 2575 passed / 0 failed。三次结果分别记录，不合并表述为一次全绿。
- 前端（`16dfdaa`）：`npx tsc --noEmit` 退出码 0；`npm run build`（`tsc -b` + vite）退出码 0；`npx vitest run` **387 passed / 50 files**（基线 364）。
- N9 平台外实跑：`python3 cli.py --input sample_quote.csv --output output/` 退出码 0，产物 sha256 `e70662b1ca7114d2281cfa1df676280bf0a585f4f26b13a9e9eb82340e84eceb` 与平台内一致；`--version` 与平台记录版本一致。
- N4 真实 GitHub 拉取：`octocat/Hello-World` 无 token 公开拉取，`master` → `7fd1a60b01f91b314f59955a4e4d4e80d8edf11d`（40 位 sha）；缺 SKILL.md 正确报 `skill_not_found`。

### 真实服务器核查（V1，证据 `receipts/V1-live-check.md`）
**uvicorn 子进程 + httpx 真实 HTTP，临时全新库，非 TestClient，非 `preview_v3.py` 演练夹具**（本轮规定预览夹具不算验证）。6/6 通过：
1. 角色裁剪：admin 13 个 key vs member 4 个 key，逐 key 对照。
2. 三个配置写端点对 member 全部 403。
3. 会话 Skill：A 导入 B 看不到；未泄漏到 manifest；删除生效；**跨会话越权删除被挡（404）**；**进程真 terminate 重启后绑定仍在**。
4. `service_url`：admin 见 13 key 含 host/user/commands，member 只见 4 key。
5. 能力来源无记录 run 返回 `{"loaded":{"status":"no_record",...},"invoked":{"status":"no_record",...}}`。
6. `do` 会话只有 calc/export；admin+admin_config 才出现 9 个配置工具；member+admin_config 仍只有 calc/export。

**证据边界**：V1 用 DummyRunner，**不调用真实模型**。凡涉及真实模型执行的链路本轮未在 V1 中验证。

## 四、需要你在服务器独立核验的

1. **配置工具闸门在真实部署身份下的行为**：确认线上 admin 的普通职能体会话工具清单确实只有 calc/export；确认配置对话入口只有 admin 可达。
2. **管理员配置对话的真实模型现场**：N11 的对话路径本机未用真实模型走通（V1 用 DummyRunner）。请在服务器用真实规划模型走一遍「对话改配置 → 表单读到改后值」。
3. **会话 Skill 的真实 GitHub 导入**：本机只验证了公开仓库无 token 路径。私有仓库 + `FACTORY_GITHUB_TOKEN` 路径未验证。
4. **N9 工具执行包下载在 Linux bwrap 下复跑**（本机是 macOS seatbelt）。
5. r2 遗留：T08 现场自动接续你已观察到首段成立；两条重启形态偶发失败仍是观察项，Linux 复验留意。

## 五、环境缺口（非代码，需用户或运维，本轮补不了）

1. **固定测试地址的实际 URL 值未提供**——N8 做的是字段与展示，值要管理员填。未填时前端显示「未登记固定地址，请管理员补齐」，不是假绿。
2. 生产部署目标登记为空。
3. `FACTORY_GITHUB_TOKEN` / `FACTORY_DEPLOY_KEY_DIR` 未配置。
4. 目标服务器上的部署脚本由运维维护，平台不传输。

## 六、未解决 / 另立（不在本轮）

- **独立 `paused` 语义**：系统无 pause 态，「暂停」等价 cancel（终态），不能原地继续。前端文案是「取消运行」，**没有假称暂停**，所以不是谎报。共识 §4/§19.3 要求暂停与取消分别记录——列为候选，本轮未做（要改核心生命周期状态机）。
- worker 内部 git 操作无平台级幂等键，靠 `base_sha` 校验 + 安全阶段选择间接保护（观察项，未扩）。
- N11 的 `binding_for` 传的 cid 来自新表 `admin_config_conversations`，不在 `agent_conversations` 里，绑定上的 `export` 工具对该会话没有对应行。配置对话用不到它，未处理。
- MFD 维持「清单局部提取」口径，未扩。F01～F06 未实现（范围锁定）。
- 后置不做：完整知识索引、跨项目知识发布、价值大屏、外部系统连接、费用治理、长期维护、语音、商城、蓝绿。

## 七、模型与流程事实

- 规划/监工与全部执行子任务**请求 `sonnet`，宿主转写元数据实际均为 `claude-opus-4-6`（1M）**，与 r1/r2 同一宿主限制。**不以 Sonnet 名义记录**，按用户授权以可用模型推进。
- 并行规模：wave 1 五个执行者 + wave 2 四个执行者，各自独立 worktree（`webuddy-next-n1`…`n11`）。共享文件（`App.tsx`、`Workbench.tsx`、`index.css`、`workspace/types.ts`、`app.py`）由主会话单点修改，子代理越界一律回报。本轮无 worktree 间写入冲突。
- 六份维度调查为**只读**子代理，不写源码。
- V1 为**独立验证员**，立场设为找实现者漏掉的东西，不与实现者共用上下文。

## 八、环境注意事项（会误导判断，务必知道）

`openai_codex` 是 `pyproject.toml` 的 `[project.optional-dependencies] codex` extra。`git worktree add` 出来的工作区各建新 `.venv`，**只装默认依赖**，于是 `test_codex_isolation_rejects_active_mcp_tools_before_dispatch` 在子 worktree 必失败（`ModuleNotFoundError: No module named 'openai_codex'`），在集成工作区却是绿的。本轮这条曾被子代理误标为「基线既有失败」，主会话又一度误判为「引入了回归」，**两个方向都错**。子 worktree 里请用 `uv run --extra codex pytest`；**权威验收一律在集成工作区跑**。

## 九、线上状态声明

本轮**未部署生产**，未改服务器。线上尚未验证的部分见第四节，**不能宣称整个平台已验收通过**。
