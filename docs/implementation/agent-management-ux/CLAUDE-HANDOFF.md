# 职能体管理入口收敛与改名 — 交接

基线 `40dcf16`（`origin/codex/autonomous-factory-v3`，实施单指定值）。候选分支 `claude/agent-management-ux`，SHA 见推送。
未访问服务器与凭据、未部署、未改集成分支、未动任何标签。会议助手已由 Codex 迁入正式站，本轮未重复迁移、未碰生产数据。

## 提交
| SHA | 内容 |
|---|---|
| `b40258c` | 后台：`PATCH /api/v4/agents/{aid}/metadata` |
| `826d2e1` | 后台回执 |
| `5f98b8b` | 前端：管理页三区块、目录卡、编辑组件、添加能力三来源 |
| `53de50e` | 前端回执 |
| `011a9a7` | 共享路由收口：取消并列「能力库」页签、旧链接归属职能体、单页签不渲染 |
| `0d8520e` | 面包屑显示当前名称并随改名即时更新；截图说明 |

## 后台（元信息）
`PATCH /api/v4/agents/{aid}/metadata`，体 `{name?, purpose?, expected_updated_at}`，`extra=forbid`。
`BEGIN IMMEDIATE` + `updated_at` CAS；只写 `name`/`purpose`/`updated_at`；审计写入 append-only 的 `agent_metadata_audit`（带禁改禁删触发器）记录操作者与前后值。
`PATCH_FIELDS` 未扩大——name/purpose 不会经模型维护 patch 进入。未触碰 `manifest.identity`、Skill 正文、工具契约、`model_settings`、`active_version` 与版本记录。

## 前端
- **目录卡**：名称、用途、主按钮「开始对话」、弱入口「管理职能体」、「⋯」菜单（`role=menuitem`，Esc 关闭且焦点归还触发按钮，实测 `aria-expanded=false`）。
- **统一编辑组件** `AgentMetadataEditor`：目录卡菜单与管理页「基本信息」共用一个；支持取消、失败保留输入、保存期间禁用防重复提交。
- **管理页**（`/agents/:agentId`，`?mode=maintain` 深链接仍可用）：基本信息 / 工作规范 / 已挂靠工具 三区块 + 添加能力；**单一外框**，正文内无第二套「你的职能体」目录（实测 `main` 内无侧栏）。
- **添加能力三来源**渐进披露，一次只展开一个。
- **聊天页**「维护方法」→「管理职能体」。

## 入口收拢 vs 新增绑定（**必须区分，按实施单要求写明**）
- **真正的角色绑定**：方法模块 → 岗位清单引用；职能包 → 既有挂靠机制。
- **仅入口收拢**：沉淀能力（v3 Capability）**没有**角色绑定机制，只能先升格为 Skill 再加入；界面按此说明，**没有假装已绑定**。其既有维护页作为次级页面保持可达。
- 上传包区分「平台导出的职能体包（会新建角色）」与「给当前角色导入的 Skill ZIP」；**没有**把「导入职能体」API 当作给当前角色添加能力，**也没有**造任意 CLI ZIP 导入接口。
- 开发成果复用真实沉淀流程，缺真实成果时给引导；未假造 run/passed/published。

## 路由与旧资产
`GROUP_TABS.agents` 去掉「能力库」；`/ability-center`、`/ability-center/packs/:packId`、`/modules`、`/capabilities` **全部保留可达**，改为无主导航的兼容次级页面，选中态归「职能体」，面包屑「职能体 › 能力资产」，**不重定向丢失、查询参数原样保留**（实测 `/modules?selected=abc` → `/ability-center?selected=abc&tab=modules`）。
AppShell 新规则：页内页签少于两个就不渲染——否则收拢后只剩一个重复页面标题的空壳条。
面包屑新增 `live` 段，由管理页用实时 agent 名填充，改名即时生效。

## 验证
- 后端 `tests/test_agent_metadata.py` **12 passed**；`-k "agent"` 回归 **176 passed**。三条变异（放开额外字段 / 去掉 CAS / 偷改 `active_version`）各自如期变红。
- 前端 `npx tsc --noEmit` 0、`npx vitest run` **415 passed / 55 files**、`npm run build` 0。
- **真实浏览器**（可丢弃本地实例，真实服务真实写入，非预览夹具）：目录→⋯菜单→Esc 焦点归还→管理页三区块→改名保存→面包屑与标题同时更新→添加能力三来源→旧深链接→`?mode=maintain`→375×812 窄屏。要点与截图说明见 `screenshots/README.md`。
- 成员边界：前端 `AgentCatalog.test.tsx` 断言 member 看不到「管理职能体」与「⋯」；后端断言 member `PATCH /metadata` 403。
- 未跑后端全量（按实施单要求）。

## 集成期发现并修的一处
B 的管理区块自己持有 `agent` 状态，与 `AgentsPage` 的 `selected` 分离，导致改名后**区块标题更新、面包屑不更新**（刷新才一致）。已把面包屑名称改由持有实时值的管理页写入，单一写入者，避免两处打架。

## 观察项（未处理，供 Codex 判断）
`frontend/src/workbench/Workbench.tsx` 里还有一条 `/ability-center`「能力中心」导航项，但**该文件已无任何引用**（App.tsx 用的是 `AppShell`），是死代码，不是活的并列入口。清理死代码超出本单范围，未动。

## 未验证
真实模型下的管理页操作、正式站数据上的表现、Linux 复跑、发布。均归 Codex。

## 截图环境
临时目录 + 端口 18931 的一次性实例，admin 账号 `demo`，两个演示职能体由脚本建立；用完已关停。脚本：`/tmp/agent_ux_demo.py`（不入库，内容见交接讨论）。未连接正式服务器。
