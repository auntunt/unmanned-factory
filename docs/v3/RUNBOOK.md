# v3 启动与运行

## 本地演练

在本仓库根目录执行：

```bash
uv sync --frozen --all-extras
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
.venv/bin/python scripts/preview_v3.py
```

打开 `http://127.0.0.1:8790`，登录 `preview / factory-preview-only`。这些是公开的本地演练账号，不能用于生产。服务只监听本机回环地址。

演练数据、仓库与工作区在 `.factory-preview/`，不进入版本控制。脚本生成明确需求和待澄清需求，经济角色故意触发一次检查失败，常规角色修复；Git、验证、费用事件、能力归档均经过真实控制链，供应商响应是演练脚本，费用为实际零外部调用。界面常驻演练标识。

演练进程可直接关闭；重启复用隔离数据，不覆盖已有用户项目。它不读取用户的工厂数据库，也不会发布 GitHub PR、发送飞书消息或创建云资源。

## 真实使用

1. 使用独立的持久数据目录与项目根目录，设置 `FACTORY_CONTROL_DATA`、`FACTORY_WORKSPACE_ROOT` 和 `FACTORY_PUBLIC_ORIGIN`。参照 [v2 运行说明](../rewrite/RUNBOOK.zh-CN.md) 创建用户和配置同源 HTTPS。
2. 执行 `.venv/bin/factory-web create-user <用户名>`，交互输入密码；随后 `.venv/bin/factory-web serve --port 8788`。一个数据库只能运行一个协调器，不使用多 worker 启动参数。
3. 打开运行配置，选择每个角色的 Provider 与实际账号可用模型 ID。Luna/Terra 是本次构建的工作分工；底层 SDK 的模型 ID 仍以实际供应商账号支持为准。先通过只读探针确认连接；macOS 系统代理可能需要显式传入服务环境，见 [小组运营说明](TEAM-OPERATIONS.md)。
4. 登记项目的独立 Git 仓库和可信检查。基线必须明确、工作区干净。启用自主策略，选择允许的风险、尝试次数和升级方式。
5. 提交一个有清晰验收的小需求验证实际模型调用。模型不报告美元费用时，可选择有界继续，配合任务数、时间、并发及尝试限制；这不构成精确美元硬封顶。
6. GitHub 发布另需 `FACTORY_GITHUB_TOKEN` 与项目发布设置。发布 PR、合并和部署是不同事件，不能将本地检查通过当成生产部署成功。

Provider SDK 与配套运行时已由 extras 安装。后续环境维护继续使用 `--all-extras`，避免普通 `uv sync` 删除这些可选依赖。运行 `.venv/bin/factory-runtime doctor --db <数据库路径> --workspace <项目根目录> --static-dir frontend/dist` 做无付费调用诊断。安装、登录提示、模型可用性与真实调用成功是不同状态。

## 从 v2 演进

本仓库基线来自 upstream 分支 `rewrite/engineering-harness-v2` 的提交 `b2425121528f2e0ed1431dcc3111b7769a6d261d`。当前本地 Git 历史是源码归档导入，基线提交 `4506eb5` 不具有 upstream 的提交祖先关系。不要直接用此本地分支覆盖 upstream；提交远端时应从上游基线创建分支并应用差异。

更新前停止旧服务，并用 SQLite backup API 备份数据库（或在所有连接关闭后完整复制数据库及所需 WAL）。v3 新表采用增量创建，保留旧项目、运行、事件和 API。旧项目没有自主策略时默认保留 supervised；不自动扩大旧项目权限。旧内容没有完整事件归档时不会虚构补全。

首次启动自动恢复未执行队列和只读规划。中断的执行/验证/发布保留现场与检查点，显示待处理。先核对已发生的写入，再重试或处理交付。

## 日常运营

- 在“团队与额度”创建成员，分配可执行项目并保存月额度。已有账号迁移为管理员，新建普通成员默认没有执行项目。登录成员共享项目与运行记录；本版面向可信小组。
- 成员、项目、工作区三个范围都参与调用前额度检查。完整用量结算，未知用量保留预留并要求管理员核对；不能把工厂额度视为供应商余额或正在执行任务的硬封顶。
- 总览关注当前异常和费用未知，不把过去已修好的失败当成待办。
- 运行详情下载 ZIP 留档，包含运行快照、Markdown 报告与全部已记录事件。
- 能力草稿自动生成，启用和版本绑定保持明确；客户配置和通用 SOP 分开维护。
- 云部署、域名、飞书等模板需要在目标环境补齐配置和结果检查；本次演练不证明这些外部操作已上线。

## 验证命令

```bash
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
git diff --check
```

测试中的模型替身不消耗真实模型额度。需要实际账户的 smoke 测试只有显式配置后才运行。验证记录见 [VALIDATION.md](VALIDATION.md)。
