# webuddy-next 本轮状态（唯一进度入口）

最后更新：2026-09-19（wave 2 推进中，N6 未回）

## 基线（已实查）
- 集成工作区：`/Users/auntlee/workspace/.factory-worktrees/v3-skills-icons`
- 本地分支 `v3-conversation-workspace` = `origin/codex/autonomous-factory-v3` = `6fe1883`，ahead/behind 0/0，干净。
- `origin/main` = `98780d8`（较早，不作起点）。服务器 `4c56632`（只读，本轮不部署生产）。
- 本机工具链：uv 0.5.15 + .venv(py3.12)，node v24.16.0；`pytest --collect-only` = 2469 collected / 2 deselected；`npx tsc --noEmit` 退出码 0。

## 模型事实
所有子代理请求 `sonnet`，宿主实际转写为 **claude-opus-4-6（1M）**，与 r1/r2 同一宿主限制。不以 Sonnet 名义记录。

## 六维差距表（六份只读调查，报告在 survey/）
| 维度 | 结论 | 本轮任务 |
|---|---|---|
| D1 成果优先展示 | `delivery_type` 前端展示**上一轮缺口已不成立**；归档/预览/下载/旧任务回查完整。缺「实际加载 Skill / 实际调用工具」摘要面板（后端 `tool.call` 事件已有真实记录，前端无聚合 UI）；非 general 操作不写 `delivery_type_inferred`；成果页无「继续修改」入口 | N6、N7（wave 2） |
| D2 Skill 与工具 | **最大缺口**：会话级 Skill 绑定**完全不存在**，当前所有会话共享所有已签署 skill（与期望相反）；GitHub URL 导入不存在；`AgentManifest.tsx:25` 写「skill 是工具」与产品定义矛盾。ZIP 导入与 SHA256 溯源已完整 | N3（wave 1）、N4/N5（wave 2） |
| D3 工具独立使用 | T04 平台内全链路已验证，工具本身零平台依赖、隔离层解耦、版本不可变。缺 CLI harness、工具版本标识、执行包下载、客户端 sha256 核对；`packs/mfd-xml-conversion/pack.json:4` purpose 写「无损转换」与工具层「局部提取」矛盾 | N9（wave 1） |
| D4 配置归管理员 | 写操作有全局 admin 中间件强制；密钥链路脱敏覆盖完整。缺 `GET /api/v2/runtime` 角色裁剪、member 可用性视图、管理员配置对话 | N1、N2（wave 1） |
| D5 干预与恢复 | **无关键缺失**，四条期望全覆盖，三层守卫只认 `needs_human` | 不派（候选 pause 见下） |
| D6 完整交付路径 | GitHub 绑定/发布/SSH 部署/版本可追溯**已实现且无 mock**。唯一代码缺口：`deploy_targets` 缺可展示的固定测试地址字段 | N8（wave 1） |

## 主会话已锁定的裁决（子代理不得推翻）
1. 会话 = 现有工作会话主键（待 N3 核实具体字段）；会话 Skill 绑定用**独立新表**，不复用 `instruction_modules`/`project_modules`；run 创建时快照到 `session_skill_snapshot`，与既有 `module_snapshot` 并列不改其语义。
2. 「固定测试地址」= `deploy_targets` 上的可展示 URL 字段；非 SSH 的静态站/Vercel 形态**不在本轮**。
3. 「沉淀能力」（v3 Capability）**保留现有标签与动作**，不强行归入 Skill 或 Tool 两类之一（术语对齐 §4「缺少类型依据的旧记录保留兼容显示」）。
4. GitHub URL 导入**在本轮范围内**（README 行明写「GitHub或完整ZIP导入」）。
5. MFD 只改措辞使其与工具层「局部提取」口径一致，不扩能力。
6. 术语对齐是最小映射：不全库重命名、不新增顶层导航、保留现有 API/表/路由/页面编号。

## 共享文件写入边界（主会话保留，任何子代理不得修改）
`frontend/src/App.tsx`、`frontend/src/workbench/Workbench.tsx`、`frontend/src/index.css`、`frontend/src/workspace/types.ts`、`factory/control/app.py`（N3 例外：仅注册新路由一处）
其余按任务单逐项声明；`Deliverables.tsx` 归 N6，`runtime_routes.py` 归 N1，`deploy_targets.py`/`ServerTargets.tsx` 归 N8。

## 任务进度
| 任务 | 维度 | worktree/分支 | 模型 | 提交 | 状态 |
|---|---|---|---|---|---|
| N1 运行环境角色边界 | D4 | webuddy-next-n1 | opus-4.6 | 27d3b25 | 已集成（含 App/Workbench 接线） |
| N2 管理员配置对话 | D4 | webuddy-next-n2 | opus-4.6 | ff583c8 | 已集成（含 agent_routes 角色接线） |
| N3 会话级 Skill 绑定（契约单） | D2 | webuddy-next-n3 | opus-4.6 | 13e823e | 已集成，契约生效 |
| N8 固定测试地址字段 | D6 | webuddy-next-n8 | opus-4.6 | 44f124e | 已集成 |
| N9 工具独立使用 | D3 | webuddy-next-n9 | opus-4.6 | 8bcf5c3 | 已集成，平台外实跑 sha256 一致 |
| N4 GitHub 来源 Skill 导入 | D2 | webuddy-next-n4 | opus-4.6 | e5e3b1c | 已集成，真实公开仓库拉取验证通过 |
| N5 前端会话 Skill 面板 + 术语呈现 | D2 | webuddy-next-n5 | opus-4.6 | 已合并 | 已集成（主会话改挂载点 + 修健壮性缺陷） |
| N6 能力来源摘要 + 继续修改入口 + 地址展示 | D1/D6 | webuddy-next-n6 | opus-4.6 | — | running |
| N7 非 general 操作 delivery_type 写入 | D1 | webuddy-next-n7 | opus-4.6 | 已合并 | 已集成 |
| N10 独立 pause 语义 | D5 | — | — | — | 候选，优先级最低 |

## 已有证据（引用，不重跑）
- r1/r2 全部证据见 `docs/implementation/webuddy-r1/`（status.md、codex-handoff.md、receipts/T01–T14.md）。
- Codex 服务器现场：T08 `run.auto_resumed` 首段成立；bwrap/隔离终端/service settings 通过。
- D5 无需新证据；D3 的 T04 全链路证据沿用，N9 只补「平台外实跑 + sha256 对照」。

## 已知阻塞（非代码，需用户/运维，本轮补不了）
1. 固定测试地址的**实际 URL 值**未提供（N8 只做字段，值要用户填）。
2. 生产部署目标登记为空。
3. `FACTORY_GITHUB_TOKEN` / `FACTORY_DEPLOY_KEY_DIR` 未配置。
4. 目标服务器上的部署脚本由运维维护，平台不传输。
以上不阻塞本轮代码与本地验证，会明确写进 codex-handoff 的外部缺口。

## 观察项（不在本轮扩）
- worker 内部 git 操作无平台级幂等键，靠 `base_sha` 校验 + 安全阶段选择间接保护。
- 两条重启形态偶发失败（r2 记录），Linux 复验留意。

## 下一步
wave 1 五单回来后：主会话核对证据 → 逐单集成到 `v3-conversation-workspace` → 依 N3 契约派 wave 2（N4/N5/N6/N7）→ 集成验证 → 推送 `codex/autonomous-factory-v3` → 写 codex-handoff。

## 集成期发现并修复的问题（主会话）
1. **N9 的「pre-existing 失败」判断错误**：`test_codex_isolation_*` 在子 worktree 报 `ModuleNotFoundError: No module named 'openai_codex'`。根因是 `git worktree add` 出来的工作区各建新 `.venv`，只装默认依赖，不含 `pyproject.toml` 的 `codex` extra。集成工作区复跑为绿 → 既不是回归也不是基线失败，是环境差异。**权威验收一律在集成工作区跑**。
2. **N2 自报的接线缺口属实**：`agent_routes.py:366` 不传 `actor_role`，对话绑定默认 member，admin 配置工具在真实对话中根本不会生效。已接线并复验。
3. **N5 的挂载点判断错误**：回执称挂在 `RunWorkspace`，但 `RunWorkspace` 只有 `runId`，没有 `agent_conversations.id`。实际正确位置是 `AgentChatPage` 的 `cv-dock`（与既有 `CapabilityPanel` 并列），该处有 `conv.id`。主会话改挂。
4. **N5 组件两处健壮性缺陷**：`setSkills(data.items)` 与 `deps.length` 均未防御，响应缺字段时整个会话页白屏（集成后 6 条既有测试变红暴露）。已加防御 + 2 条回归测试。

## 集成验证记录（集成工作区，`-p no:randomly`）
- 定向后端（wave 1 合并后）：389 passed / 1 skipped，退出码 0
- 对话与配置：47 passed，退出码 0
- delivery 相关（N7 合并后）：182 passed，退出码 0
- 前端：`npx tsc --noEmit` 退出码 0；`npx vitest run` 376 passed（基线 364）→ 加回归测试后 378
- 待做：N6 合并后跑一次后端全量 + 前端全量

