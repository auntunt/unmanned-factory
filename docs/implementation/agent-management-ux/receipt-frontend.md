# 前端改造回执 — 职能体管理入口收敛

## 模型与提交

- 实际模型：claude-opus-4-6 (1M context)
- 提交 SHA：5f98b8b
- 分支：claude/agent-management-ux
- 基线：40dcf16

## 改动文件

| 文件 | 改动 |
|------|------|
| `frontend/src/workbench/AgentMetadataEditor.tsx` | 新建：共享编辑组件（名称+用途） |
| `frontend/src/workbench/AgentMetadataEditor.test.tsx` | 新建：6 个测试（保存/取消/失败保留输入/防重复提交/409 冲突/空名禁用） |
| `frontend/src/conversation/AgentCatalog.tsx` | 重写：目录卡加"..."菜单、"管理职能体"链接、菜单内编辑 |
| `frontend/src/conversation/AgentCatalog.test.tsx` | 新建：8 个测试（渲染/权限/菜单打开/Esc 关闭焦点归还/键盘导航/内联编辑） |
| `frontend/src/conversation/AgentChatPage.tsx` | "维护方法" 改为 "管理职能体"，链接到 /agents/:id |
| `frontend/src/conversation/conversation.css` | 新增 .cv-card-menu-* 和 .cv-metadata-editor 样式 |
| `frontend/src/workbench/AgentsPage.tsx` | 重写管理页：移除 AgentList 左侧栏和 AgentChat 双模式组件，改为三区块管理布局 + AddAbilityPanel |
| `frontend/src/workbench/AgentManagement.test.tsx` | 新建：6 个测试（三区块存在/无左侧栏/三来源渐进披露/编辑按钮/上传源说明/mode=maintain 深链接） |
| `frontend/src/workbench/agents.css` | 新增 .agent-mgmt/section/info/source/tool 系列样式 |
| `frontend/src/workbench/AddAgentAbility.test.tsx` | 适配新管理页结构（移除旧模式切换断言） |
| `frontend/src/workbench/project-files.test.tsx` | 移除引用已删除 AgentChat 的测试用例 |
| `frontend/src/conversation/AppShell.test.tsx` | 适配单标签页不渲染 sub-nav 的新行为 |

## 区块落实方式

### 1. 基本信息区
- 显示名称、用途、版本、更新时间
- 管理员可点"编辑名称与用途"打开 AgentMetadataEditor
- 调用 `PATCH /api/v4/agents/{aid}/metadata`（按任务 A 锁定的契约）
- "开始对话"主按钮 + 导出职能包链接

### 2. 工作规范区
- 直接渲染 AgentManifest（岗位清单 — 已启用 Skill、身份段、验收断言）
- 直接渲染 AgentEvolution（进化提案）
- 版本维护入口在清单内

### 3. 已挂靠工具区
- 从 `/api/v4/capability-packs/bindings/{agentId}` 加载
- 显示工具名、固定版本、环境状态（就绪/不可用+缺失依赖）
- "管理"按钮跳转 `/ability-center/packs/:packId`，沿用现有授权与确认流程

## 三来源落实方式

### 上传包
- 上传 Skill ZIP 文件，调用 `/api/v4/agents/{aid}/abilities`
- **明确说明**：平台导出的职能体包（会新建角色）应使用列表页"导入职能体"入口
- **明确说明**：当前没有任意 CLI ZIP 初始导入接口，不假造

### 团队已有能力
- 加载方法模块（Skill）列表 → 链接到能力库模块详情
- 加载沉淀能力列表 → 链接到能力库能力详情
- **入口收拢，不是新增绑定能力**：
  - 模块的绑定通过上方"工作规范"区的岗位清单完成（已有机制）
  - 沉淀能力需经"升格为 Skill"+ 进化提案流程才能加入职能体
  - 职能包通过挂靠机制绑定，入口在职能包目录
  - 三种实体语义未混写

### 开发成果
- 加载已挂靠的 PackBinding 列表
- 无成果时给出明确引导（先在项目中完成 → 沉淀为职能包 → 发布版本 → 挂靠）
- **未假造 run/passed/published**，状态来自真实服务

## 哪些能力只是「入口收拢」而非「新增绑定能力」

1. **沉淀能力**（Capability）：仅在"团队已有能力"中展示链接和来源运行。绑定到职能体需经升格为 Skill + 进化提案流程，本轮未新增绑定 API。
2. **方法模块**（Module/Skill）：在"团队已有能力"中展示列表，实际加载通过上方岗位清单的 select 完成（已有机制），本轮未新增绑定机制。
3. **职能包**（Pack）：在"已挂靠工具"区展示已绑定的，"管理"跳转到 `/ability-center/packs/:id`，沿用现有 PackBind 流程。
4. **上传包**：调用已有的 `/api/v4/agents/{aid}/abilities` 接口，不是新增导入路径。

## 聊天页改动
- "维护方法" → "管理职能体"
- 链接从 `/agents/:id?mode=maintain` 改为 `/agents/:id`（管理页不再依赖 mode 参数）
- 普通成员在聊天页看不到管理入口（`isAdmin` 检查未变）

## 验证命令与退出码

```
cd frontend && npx tsc --noEmit       # 退出码 0
cd frontend && npx vitest run          # 55 files, 415 tests passed, 退出码 0
cd frontend && npm run build           # tsc -b && vite build，退出码 0
```

## 需要主会话接线的路由/导航点

1. **nav-config.ts 已由主会话完成**：能力库页签已删除、agents 详情面包屑已更新
2. **App.tsx 无需改动**：路由结构不变，`/agents/:agentId` 仍由 AgentsPage 接收
3. 旧链接（`/ability-center`、`/modules`、`/capabilities`）仍可到达，未删除路由

## 未验证项

- 真实浏览器端到端操作（菜单焦点归还在 jsdom 中已测试，但实际浏览器行为未验证）
- 窄屏响应式布局（CSS media query 已写，未通过真实设备验证）
- 后端元信息接口（PATCH /metadata）的实际联调（由任务 A 并行开发中）
