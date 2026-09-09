# 自动编码接续与项目导入：2026-09-10 增量实现

本轮在现有工作台、六阶段展示、执行 SDK、工作区、项目知识、职能体版本和交付归档上增量实现。没有新增一套角色流程，也没有重新建设模型配置或本地计费系统。

## 使用方式

### 上传已有项目

管理员在「项目 → 新建工作区 → 工程来源 → 上传项目 ZIP」选择项目包，填写名称，可选关联职能体。上传成功后进入项目，用自然语言描述维护需求。

导入保存项目初始 Git 版本、原始 ZIP 和静态识别报告。识别出的文档、依赖声明及可能的入口用于辅助后续规划。报告位于 `.webuddy/import-report.json`，初始运行状态是 `not_run`：导入成功不代表依赖已经安装或程序已经跑通。后续编码任务会被要求检查现有运行方式、建立行为基线并验证具体修改。

导入限制为 ZIP 20 MiB、展开 100 MiB、最多 5000 条目。路径越界、符号链接、重复路径和异常压缩包被拒绝。Git 内部文件、常见凭证文件、生成依赖目录及平台保留目录不会进入工作树。原始包保存在工作树外，上传阶段不执行项目脚本。

### 任务执行中补充需求

在职能体任务对话中直接发送补充即可。消息显示待接续状态，当前任务成功结束并释放执行槽后，系统自动创建后续任务。页面自动跟随最新任务，不需要再次发送“继续”。

接续使用上一轮已验证的提交和工作区，核对项目、对话及 Git 仓库归属，不改变原项目主分支。职能体版本和模型配置沿用冻结快照；执行仍遵守项目策略与当前成员权限。

这是成功边界上的自动接续，不是中断正在生成的模型并实时注入消息。遇到需要澄清、失败、取消或权限变化时，不会通过自动反馈绕过这些状态；待处理补充仍会保留。

## 验证与证据

Claude 隔离终端会记录真实命令结果，包括退出码、超时、耗时及有界脱敏输出。这些观察随执行尝试保存，供独立验证使用；命令成功本身不等于业务验收通过。

独立验证的证据摘要限制在 24,000 字符，优先保留任务和检查状态、失败命令；省略的命令详情会明确计数，完整运行记录不因此被删除。原有可信检查继续执行。

本轮验证使用临时 Git 仓库、真实本地命令及脚本 Provider。项目导入集成测试实际执行 Python CLI 并检查输出，之后保存交付成果。反馈测试覆盖并发采用、失败边界、权限撤回、重启恢复和上一轮代码保留。另以独立本地账号完成浏览器登录、ZIP 上传、导入结果与项目维护入口检查。

这些结果不代表真实模型已经完成复杂业务验收，也不代表生产服务器已部署。Claude 终端仍依赖 Linux 上可用的 bubblewrap；本轮没有在 macOS 上宣称验证了该隔离运行环境。

最终验证结果：后端相关回归 **296 passed**（101.60 秒；一个既有 Starlette 弃用提示），前端 **137 passed**，TypeScript 与生产构建通过，`git diff --check` 通过。这是本轮相关模块回归，不是全仓库所有测试或生产压力测试。

后端复现命令（仓库根目录）：

```sh
GIT_CONFIG_GLOBAL=/dev/null .venv/bin/pytest -q tests/test_control*.py tests/test_agent*.py tests/test_autonomous_service.py tests/test_project*.py tests/test_managed_workspaces.py tests/test_queue_continuation.py tests/test_gateway_billing.py tests/test_team_governance.py tests/test_runtime_execution.py tests/test_command_evidence.py tests/test_verification_evidence.py tests/test_claude_terminal.py --tb=short
```

前端在 `frontend` 目录运行 `npm test -- --reporter=dot` 和 `npm run build`。浏览器使用临时数据目录和禁止模型调用的本地服务，验收后已关闭服务；没有修改生产数据或模型凭证。

## 当前文档口径

`docs/rewrite/STATUS.md`、`docs/vertical-agents/IMPLEMENTATION-STATUS.md` 等带日期的交付记录保留当时事实，不能据其中的待办推断当前代码缺失。当前计费行为以 [计费边界](gateway-billing.md) 为准；本轮同步相关旧测试，保留权限、取消、冻结配置和成果完整性断言。
