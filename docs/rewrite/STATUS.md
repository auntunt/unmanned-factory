# 本次交付状态

日期：2026-09-08。本次交付在工程流程与 Project Agent 的基础上，完成 **全新工作台、运行环境诊断和模型配置、现有服务器更新流程**。本地实现和验证不等于三家真实供应商认证或服务器部署完成。

## 已实现

| 诉求 | 本次代码与行为 |
| --- | --- |
| 登录保护 | `auth.py`、`app.py`：管理员命令创建账号、密码哈希、过期会话、限流、CSRF、同源和 HTTPS 配置校验；历史 API 也经过登录保护 |
| 全新工作台 | `frontend/src/workbench`：登录、待办概览、项目登记与设置、逐参数检查编辑器、需求、任务依赖图、问答、审计与交付；路由懒加载，移动端折叠菜单 |
| 运行环境管理 | `runtime.py`、`runtime_routes.py`：SDK/API/配套程序诊断，认证线索与真实连接测试分层，四角色模型配置持久化、版本冲突校验及每次计划冻结；GET 不调用模型 |
| 安装与诊断 | `runtime_cli.py`、`deploy/install-control.sh`：全部 extras 安装、直接虚拟环境启动、只读 doctor；更新现有 factoryweb，保留 Caddy/env/数据库，不启用第二服务 |
| 自动需求梳理 | `planning.py`：模型读取仓库后生成结构化计划；缺信息必须澄清，计划版本与审批绑定 |
| 可视化 | 任务 DAG 和可点击的范围/验收详情；支持补充需求并重新规划，尚非完整 ShowMe 原型编辑器 |
| 多 SDK | `providers.py`、`sdk_worker.py`：Claude、Codex、DSH 可选适配、进程通信、取消、超时、事件与用量归一化；DSH 不支持只读规划 |
| 模型和子代理 | `execution.py`：cheap/standard/strong 策略路由，可配置任务数/并发/阶段时限；依赖与重叠范围串行，未知费用默认停止，可显式允许受执行限制的多阶段继续并禁止自动发布 |
| 工程验证 | 新 worktree、范围检查、Git 元数据保护、可信检查、验证前后内容核对、集成后回归、提交 SHA 绑定 |
| 留痕与问答 | `store.py`：SQLite 追加写事件、脱敏、真实消息投影、Markdown 导出、重启转人工核对；大输出有明确截断 |
| GitHub | `github.py`：验证分支推送、PR 创建/复用；Issue 签名与重复投递校验、确定性分流、内容更新撤销旧待执行计划 |
| Project Agent 增量 | `knowledge.py`、`codegraph.py`、`context.py`、`history.py`、`project_routes.py` 及工作台组件：版本化工程记忆、候选/活动区分、固定 Git 对象代码图与源码链接、显式 TeamAI 文档预览/应用、运行上下文冻结、独立 PR 合并观察与经祖先校验的历史检索 |
| 文档与迁移 | 同类项目取舍、分层设计、接口契约、部署说明；原 CLI 和历史数据保留 |

Astra 负责架构建议和困难边界审查，主代理负责契约、集成、复查和提交；Luna 子代理承担知识库、代码图、GitHub 证据、工作台及测试等明确任务，并按审查结果完成针对性修复。React 检查推动了表单草稿保留、项目切换取消请求、运行上下文刷新与证据 SHA 展示修复。

## 验证

- 后端主回归：**1416 passed / 38 skipped / 2 deselected**。命令：`GIT_CONFIG_GLOBAL=/dev/null .venv/bin/pytest -s -m 'not smoke' --ignore=tests/test_permission_hook.py --ignore=tests/test_proc_fd_leak.py`。使用干净 Git 全局配置匹配测试 fixture 的默认分支；没有为环境差异修改产品行为。
- 新运行配置与工作台集成专项已通过，覆盖真实临时 Git 仓库中的两批依赖任务、未知费用策略、旧计划模型冻结、配置并发冲突、项目编辑锁与 HEAD/base 一致性。
- 主回归后完成超时和异常费用修复，受影响执行专项：**27 passed**，含 16 个新增用例。本层 Git 子调用共享剩余时限，最终守卫超时不能宣布交付；NaN/Inf/负数等费用视为未知，合计溢出明确失败。既有底层本地检查需等待当前系统调用返回后才能报告超时，不声称可强制抢占全部文件操作。
- 前端：**110 passed**，TypeScript 与生产构建通过。主入口去除 Ant Design 运行依赖，任务和配置详情按需加载；主 JavaScript 约 225 kB，不再触发原主包的 500 kB 提示。
- 三家 SDK 在本地同一虚拟环境安装并完成包导入和配套程序诊断：Codex 0.147.0、Claude 0.2.152、DSH 0.1.2rc1。没有自动调用付费模型；这些本地结果不能代替服务器账号和模型连接验证。
- `uv lock --offline --check`、安装脚本语法和差异检查通过。doctor 不修改生产数据库，连接探测响应正文与异常详情不会进入审计记录。
- `permission_hook` 与进程文件描述符测试未纳入上述主回归；此前未修改基线在同一环境为 9 failed / 9 passed，失败来自 AF_UNIX 权限限制。本轮未修改相关模块或绕过限制。
- 未完成真实浏览器点选与截图验收：没有可用 Chromium，安装下载超时。使用现有前端测试及 HTTP/Git 集成测试，手机视觉和登录后的浏览器全流程仍需验收。
- 服务器 SSH 返回 `Network is unreachable`，发生在密钥认证之前。本轮没有远程安装、重启服务、改变 Caddy 或确认公网版本；可执行部署脚本与 Hermes 交接已提供。

## 仍需完成

1. **真实运行认证**：逐家配置账号，运行真实修复、取消、超时和权限拒绝用例；三家模型的可用型号与成本不能由当前环境代替确认。
2. **隔离 worker**：控制面与执行器目前同一 OS 用户。worktree、工具钩子和 SDK sandbox 不能被宣传为远程多租户隔离；尤其 DSH 需要额外的受控执行环境。
3. **持续自主执行**：持久队列/租约、失败任务续跑、有限修复与模型升级、可信价格表、跨运行仓库协调、自动同步远端基线尚未实现。
4. **完整证据存储**：大日志无损归档、事件保留策略和灾备恢复；当前保存有界脱敏记录，不能称为全部原始字节永久保存。
5. **工程反馈循环**：CI 检查与 PR review 回流、独立语义审查、视觉预览反馈、Issue 自动评论和重复修复防护。
6. **团队与管理**：账号恢复、项目 ACL、GitHub App 短期凭据与远程沙箱。项目设置编辑已在本轮实现。
7. **大仓库与团队互通**：更丰富的代码语义索引、增量更新、检索质量评测，以及 TeamAI 原生团队资源协议适配；当前只支持显式 Markdown 交换，不运行其 CLI/hooks/MCP。

新版工作台说明见 [WORKBENCH.zh-CN.md](WORKBENCH.zh-CN.md)，服务器交接见 [HERMES-HANDOFF.md](../../deploy/HERMES-HANDOFF.md)。运行手册见 [RUNBOOK.zh-CN.md](RUNBOOK.zh-CN.md)，Project Agent 实践说明见
[PROJECT-AGENT.zh-CN.md](PROJECT-AGENT.zh-CN.md)，后续阶段验收见 [PLAN.zh-CN.md](PLAN.zh-CN.md)。默认交付 PR，不自动合并主分支。
