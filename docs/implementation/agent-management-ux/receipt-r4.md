# R4 回执：恢复维护功能入口 + 管理页呈现收尾

## 实际模型

claude-opus-4-6[1m]

## 提交

`9de1798` on `claude/agent-management-ux`，基于 `d047fee`。

## 恢复的四项能力及各自入口

| 能力 | 入口位置 |
|------|----------|
| 模型配置（provider/model/阶段覆盖） | 管理页 > 高级设置 / 维护 > ModelSettings 表单 |
| 维护对话（生成草稿） | 管理页 > 高级设置 / 维护 > MaintenancePanel 文本框 |
| 应用草稿（DraftCard） | 管理页 > 高级设置 / 维护 > 维护对话产出草稿后出现 |
| 版本回退 | 管理页 > 高级设置 / 维护 > ModelSettings 内「恢复历史版本」下拉 |

以上均作为 `<details>` 折叠区域嵌入 AgentManagementPage，不恢复双壳/第二侧栏。

## 项目开发入口

管理页基本信息区新增「创建样例项目」按钮，复用 `ProjectForm`（传入 `agentId` + `uploadFirst`），创建后导航到项目详情页。agent 上下文通过 `agentId` prop 保持。

`project-files.test.tsx` 新增行为测试 `creates a sample project within the current agent context from the management page`，验证 ProjectForm 在提供 agentId 时将 agent_id 传入导入接口。

## 折叠后默认呈现

- 基本信息：始终展开（名称、用途、版本、编辑按钮、开始对话、创建样例项目）
- 工作规范：默认展示 `ManifestSummary`（身份段前 120 字 + skill/断言/revision 计数），编辑正文和进化提案折入 `<details>` 「编辑工作规范」
- 已挂靠工具：始终展开
- 添加能力：始终展开（仅 admin）
- 高级设置 / 维护：折叠（仅 admin）

## 文案变更

| 位置 | 旧文案 | 新文案 |
|------|--------|--------|
| 上传包说明 | 请使用列表页的"导入职能体"入口 | 若要整包导入一个新职能体，请返回职能体列表使用「导入职能体」按钮 |
| 添加能力描述 | 选择来源后展开对应表单。一次只显示一个来源 | 从以下来源为当前职能体添加能力 |
| 团队已有能力说明 | 沉淀能力需经升格流程成为 Skill 后加入 | 沉淀能力需要先升格为 Skill 才能加入 |
| 沉淀能力注释 | 沉淀能力本身面向项目使用。要将其加入...进化提案流程 | 沉淀能力目前用于项目运行。要加入职能体，请在详情页点击「升格为 Skill」 |
| 模块选择提示 | 选好后在上方「工作规范」的岗位清单里加载它 | 选好后展开「编辑工作规范」在岗位清单里加载 |
| 待验证工具注释 | 打开后按原有流程验证并发布，完成后回到这里挂靠 | 完成验证和发布后即可挂靠 |
| 已挂靠工具描述 | 通过职能包挂靠机制安装的独立工具。版本在挂靠时冻结；升级和解除挂靠沿用现有确认流程 | 已安装的独立工具。版本在挂靠时锁定，可在下方升级或解除 |
| 开发成果描述 | 复用真实开发成果沉淀的工具包：从运行成果中选择... | 从开发运行中沉淀的工具包：选择成果... |

导入职能体按钮在 `/agents` 列表页 `PageHeader` 的 actions 区已可达（admin only）。

## 验证命令 + 退出码

| 命令 | 退出码 |
|------|--------|
| `cd frontend && npx tsc --noEmit` | 0 |
| `cd frontend && ./node_modules/.bin/vitest run src/workbench/CodexAgentUxReview.test.tsx` | 0 (5 passed) |
| `cd frontend && ./node_modules/.bin/vitest run` | 0 (56 files / 421 passed) |
| `cd frontend && npm run build` | 0 |

## 额外修复

- `PackBinding.environment.stale` / `stale_reason` 访问在 `tsc -b` 下类型报错（d047fee 引入），通过类型断言修复。
- `MaintenancePanel` 中 draft API 响应守卫：非法响应（缺 `revision`）设为 null 避免下游 crash。
- `ModelSettings` 中 `draft?.patch?.model_settings` 使用安全导航防止 patch 为 undefined 时的 TypeError。

## 未做项

- 截图 README 仍为文字索引（不要求客户数据进 Git）。
- 未修改后端、nav-config.ts、AppShell.tsx、CodexAgentUxReview.test.tsx。
