# 本次交付状态

日期：2026-09-08。本次交付为 **P0 设计 + P1 工程纵向流程实现，并增补 Project Agent 的知识、代码图和交付核验边界**。已完成本地集成与回归；这不等于三家真实供应商端到端认证或生产上线。

## 已实现

| 诉求 | 本次代码与行为 |
| --- | --- |
| 登录保护 | `auth.py`、`app.py`：管理员命令创建账号、密码哈希、过期会话、限流、CSRF、同源和 HTTPS 配置校验；历史 API 也经过登录保护 |
| 基本控制台 | `frontend/src/workspace`：工程登记、需求、计划、任务图、状态、问答、审计、交付证据 |
| 自动需求梳理 | `planning.py`：模型读取仓库后生成结构化计划；缺信息必须澄清，计划版本与审批绑定 |
| 可视化 | 任务 DAG 和可点击的范围/验收详情；支持补充需求并重新规划，尚非完整 ShowMe 原型编辑器 |
| 多 SDK | `providers.py`、`sdk_worker.py`：Claude、Codex、DSH 可选适配、进程通信、取消、超时、事件与用量归一化；DSH 不支持只读规划 |
| 模型和子代理 | `execution.py`：cheap/standard/strong 策略路由，最多 20 个任务，默认并发 2，依赖与重叠范围串行 |
| 工程验证 | 新 worktree、范围检查、Git 元数据保护、可信检查、验证前后内容核对、集成后回归、提交 SHA 绑定 |
| 留痕与问答 | `store.py`：SQLite 追加写事件、脱敏、真实消息投影、Markdown 导出、重启转人工核对；大输出有明确截断 |
| GitHub | `github.py`：验证分支推送、PR 创建/复用；Issue 签名与重复投递校验、确定性分流、内容更新撤销旧待执行计划 |
| Project Agent 增量 | `knowledge.py`、`codegraph.py`、`context.py`、`history.py`、`project_routes.py` 及工作台组件：版本化工程记忆、候选/活动区分、固定 Git 对象代码图与源码链接、显式 TeamAI 文档预览/应用、运行上下文冻结、独立 PR 合并观察与经祖先校验的历史检索 |
| 文档与迁移 | 同类项目取舍、分层设计、接口契约、部署说明；原 CLI 和历史数据保留 |

Astra 负责架构建议和困难边界审查，主代理负责契约、集成、复查和提交；Luna 子代理承担知识库、代码图、GitHub 证据、工作台及测试等明确任务，并按审查结果完成针对性修复。React 检查推动了表单草稿保留、项目切换取消请求、运行上下文刷新与证据 SHA 展示修复。

## 验证

- 后端主回归：**1382 passed / 38 skipped / 2 deselected**。命令：`.venv/bin/pytest -s -m 'not smoke' --ignore=tests/test_permission_hook.py --ignore=tests/test_proc_fd_leak.py`。真实模型 smoke 未运行，隔离/环境条件不足的测试保留 skip，不把它们算作通过。
- 随后补充并复跑 Project Agent 与关联控制模块专项：**67 passed**，包含新增的历史合并事实进入上下文、人工改写后排除用例。
- `permission_hook` 与进程文件描述符测试未纳入上述主回归；此前在未修改基线的同一执行环境单独核对为 **9 failed / 9 passed**，失败来自 AF_UNIX socket 权限限制。本轮未修改这些模块，也未尝试绕过该限制。
- 前端：**103 passed**，TypeScript 与生产构建通过；仍有大于 500 kB 的 bundle 提示，代码分包属于后续优化。
- 真实仓库的只读索引检查：241 个文件、2872 个节点、6263 条边；过大/敏感路径显式跳过。没有调用模型或执行仓库脚本。
- 未执行真实付费模型调用或已认证的供应商任务；GitHub HTTP 使用替身时只能证明协议分支，不能宣称远程合并成功。
- 未进行真实浏览器点选验收：环境缺少 agent-browser 与 Chromium，使用了组件渲染/纯函数测试及 HTTP 集成测试；没有公网部署。

## 仍需完成

1. **真实运行认证**：逐家配置账号，运行真实修复、取消、超时和权限拒绝用例；三家模型的可用型号与成本不能由当前环境代替确认。
2. **隔离 worker**：控制面与执行器目前同一 OS 用户。worktree、工具钩子和 SDK sandbox 不能被宣传为远程多租户隔离；尤其 DSH 需要额外的受控执行环境。
3. **持续自主执行**：持久队列/租约、失败任务续跑、有限修复与模型升级、可信价格表、跨运行仓库协调、自动同步远端基线尚未实现。
4. **完整证据存储**：大日志无损归档、事件保留策略和灾备恢复；当前保存有界脱敏记录，不能称为全部原始字节永久保存。
5. **工程反馈循环**：CI 检查与 PR review 回流、独立语义审查、视觉预览反馈、Issue 自动评论和重复修复防护。
6. **团队与管理**：账号恢复、项目设置编辑、项目 ACL、GitHub App 短期凭据与远程沙箱。
7. **大仓库与团队互通**：更丰富的代码语义索引、增量更新、检索质量评测，以及 TeamAI 原生团队资源协议适配；当前只支持显式 Markdown 交换，不运行其 CLI/hooks/MCP。

运行手册见 [RUNBOOK.zh-CN.md](RUNBOOK.zh-CN.md)，Project Agent 实践说明见
[PROJECT-AGENT.zh-CN.md](PROJECT-AGENT.zh-CN.md)，后续阶段验收见 [PLAN.zh-CN.md](PLAN.zh-CN.md)。默认交付 PR，不自动合并主分支。
