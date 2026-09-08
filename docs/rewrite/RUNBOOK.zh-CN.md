# 工程工作台运行说明

本入口面向可信工程师管理的专用机器。使用单个服务进程。架构与后续阶段见 [规划](PLAN.zh-CN.md)，交付边界见 [状态](STATUS.md)。
Project Agent 的记忆、固定提交代码图、冻结上下文和合并核验边界见
[实践说明](PROJECT-AGENT.zh-CN.md)；本增量最终 root regression 仍为 pending。

## 1. 安装

要求 Python 3.12+、uv、Git、Node.js/npm。以下命令从本仓库根目录开始执行：

```bash
uv sync --extra codex --extra claude --extra dsh
cd frontend
npm ci
npm run build
cd ..
```

三家 SDK 是可选依赖，只安装所用的 `--extra` 即可。版本记录在 `uv.lock`；真实安装接口核对版本为 Codex 0.147.0、Claude 0.2.152、DSH 0.1.2rc1。SDK 安装成功不表示账号或指定模型已获得权限。

## 2. 配置和创建账号

```bash
export FACTORY_CONTROL_DATA="$HOME/.factory/control"
export FACTORY_WORKSPACE_ROOT="$HOME/projects"
export FACTORY_PUBLIC_ORIGIN="http://127.0.0.1:8788"
export FACTORY_TASK_TIMEOUT="600"

export FACTORY_PLANNER_PROVIDER="codex"
export FACTORY_PLANNER_MODEL="填写账号实际可用的规划模型 ID"
export FACTORY_CHEAP_PROVIDER="codex"
export FACTORY_CHEAP_MODEL="填写账号实际可用的便宜模型 ID"
export FACTORY_STANDARD_PROVIDER="codex"
export FACTORY_STANDARD_MODEL="填写账号实际可用的常规模型 ID"
export FACTORY_STRONG_PROVIDER="codex"
export FACTORY_STRONG_MODEL="填写账号实际可用的强模型 ID"

uv run factory-web create-user owner
uv run factory-web serve
```

`create-user` 通过隐藏输入读取并确认密码，至少 12 字符。没有公开注册入口。同一实例的账号共享项目；目前没有项目级权限分配、自助改密或账号恢复界面。数据目录放在被执行仓库之外。

分别按照供应商 SDK 的认证说明配置运行服务的账号。不要将密钥填写到需求、Issue、前端或项目检查命令里。DSH 额外要求显式 `DSH_HOME`，不同隔离域使用不同目录；当前 DSH 适配不支持只读规划，因此规划角色选 Codex 或 Claude。

本次开发使用 Luna 子代理，并不意味着你的供应商账号存在名为 Luna 的可调用模型。四个档位由运行时环境变量映射，前端显示实际映射。未填写模型时会明确停止，不会自动猜型号。

## 3. 第一次运行

1. 打开 `http://127.0.0.1:8788` 并登录。
2. 登记服务器上已有的 Git 仓库。路径必须是 `FACTORY_WORKSPACE_ROOT` 的独立子目录，基线必须是存在的本地分支。
3. 配置可信检查，例如 `{"unit":["uv","run","pytest","-q"]}`。检查命令由工程师维护；模型只能引用检查名称。
4. 提交需求，查看待澄清问题、任务依赖图、改动范围和验收条件。
5. 补充信息会生成新版本。确认当前计划后执行；未通过检查会停止并保留工作区和日志。
6. 查看事件、问答和验证证据，下载工程记录；验证通过后才能发布 PR。

任务默认最多并发 2 个，范围重叠的任务串行。工作区在目标仓库旁的 `.factory-<runid>-...` 目录；交付分支为 `factory/<runid>`，子任务分支为 `factory/<runid>-task/<taskid>`。不要在运行中清理这些目录或移动基线。系统使用本地基线，不会在后台自动拉取最新 main；工程师应在新运行前刷新本地仓库。

当前没有自动修复失败任务、续跑剩余 DAG 或按价格表估算 Codex/DSH 成本。实际美元未知时不会把它记为零：停止下一批派发，完整完成的批次保留验证结果并禁用自动发布，交由人工核对。预算是依据已报告成本停止后续派发的阈值，不是供应商侧硬限额。

## 4. GitHub 交付与 Issue

通过服务进程环境配置 `FACTORY_GITHUB_TOKEN`，令牌只授权需要处理的仓库，并具备推送代码、创建 PR 的权限。项目中的 `owner/repo` 是发布目标；每个仓库只能登记一次。发布会推送已验证 SHA、创建或复用同一运行的 PR，不执行主分支合并。

Webhook 配置：

- URL：`https://你的域名/api/v2/github/webhook`
- Content type：`application/json`
- Secret：与服务端 `FACTORY_WEBHOOK_SECRET` 一致
- Events：Issues

Webhook 按签名、已登记仓库、delivery id 和内容版本校验。默认只分析并等待确认。自动执行必须同时满足：项目开启 `auto_issues`、Issue 含维护者控制的 `factory-ready` 标签、规格没有未决问题、任务低风险且具备可信检查。`auto_publish` 另行显式开启。

Issue 出现新版本时撤销尚未完成的旧运行；新版本必须人工核对前次变更，不能连续自动派发。已经在发布中的操作可能完成，其证据会保留。当前不向 Issue 自动发评论，也不自动解决 PR review 或 CI 失败。

## 5. HTTPS 与历史界面

服务固定绑定 `127.0.0.1:8788`。公网域名必须设置完整 HTTPS 源，例如 `FACTORY_PUBLIC_ORIGIN=https://factory.example.com`，再用 [Caddy 示例](../../examples/control/Caddyfile.example) 反代。前端与 API 使用同一源。用户名密码登录、会话 Cookie、来源校验和 CSRF 都由新入口提供。

不要再把旧 `factory api` 服务直接转发到公网。历史页面位于 `/control-room` 和 `/admin`，通过新入口读取已有数据；可配置 `FACTORY_AUDIT_DB` 与 `FACTORY_QUEUE`。历史数据库不迁移、不改写。

登录是访问控制，Git worktree 是文件隔离，都不能代替 OS 沙箱。当前 provider 子进程和控制面同一 OS 用户；DSH 没有适配器已验证的写入沙箱。多用户公网服务上线前，需要规划中的隔离 worker 和凭据域拆分。

## 6. 记录与恢复

`users.db` 保存账号与会话，`control.db` 保存项目、计划、运行和追加写事件。备份前停止服务，同时保留数据库及可能存在的 WAL 文件；备份与工作区也应限制文件权限。

重启后，未完成运行标为需要人工核对，不自动重新付费执行。发布失败可在核对日志后重试，同一运行会复用已有 PR。人工确认之前不要删除失败工作区。

记录包括用户原文、公开助手消息、工具事件、检查、提交、发布和用量；不记录模型私有推理。凭据在落库前脱敏。当前检查输出每项保留前 4000 字符，SDK 事件字段与数据库长文本也有大小上限，超限显示截断标记。完整大输出归档属于 P2，不应将 P1 称为无损日志存储。

## 7. Project Agent 日常使用

Project Agent 与已有项目/运行入口使用同一认证网关。建议按以下顺序操作：

1. 在工程页确认 `repository`、workspace 和 `base_branch`；代码索引只解析该分支的 `refs/heads/{base_branch}`，不读取脏工作区文件。规划前后的 checkout 校验仍要求 `HEAD` 等于 baseline SHA 且工作区 clean，脏工作区不能进入已审批规划。
2. 打开“档案/知识”页维护工程使命、约束和知识条目。`active` 是可进入规划上下文的已审核记忆；`candidate` 只供人工复核，不能当成事实。更新使用 revision CAS，历史版本和 provenance 保留。
3. 运行 `POST /api/v2/projects/{pid}/code-index` 建立快照，再用 code-search/code-graph 查看定位证据。快照固定提交 SHA，有界且可重建；Python 声明/导入可标 syntax，静态调用边与 JS/TS 正则结果标 heuristic，均不证明运行时行为。
4. 需要引入团队文档时，在导入页粘贴明确的 JSON bundle，先调用 `POST /api/v2/projects/{pid}/wiki-import/preview`，人工检查文档路径、hash 和 warning，再调用 apply 选择条目。导入结果是 `teamai_import` 的 hypothesis/candidate。
5. 规划前由系统组装同一 SHA 的有界上下文并冻结到运行记录；后续知识或索引变化不会改写该计划版本。审批时若 SHA 漂移，核对本地仓库并补充需求重新规划，建议同时更新索引。已核验、未改写且属于当前提交祖先的合并事实可作为历史记录检索，但不代表当前行为仍然正确。

TeamAI 文档只按显式 bundle 作为数据导入。不要运行原生 CLI，不加载 resource injection，不执行文档中的 hooks、MCP、YAML、脚本或任意 URL；索引和导入阶段没有模型调用。

## 8. PR 合并确认

发布成功不代表已合并。人工操作或签名 `pull_request.closed` webhook 触发独立 GET 核验，服务必须再次确认目标仓库、base/head ref、head SHA、merged、merged_at 和 merge commit SHA。只有核验通过，才为本次 run 记录一条简洁的 active fact；仅 closed、错误仓库、head 漂移、坏 SHA 或网络失败都不算合并。

此核验流程不会 push、merge、close、comment，也不会自动合并 main。系统不会自动拉取远端；工程师应先更新本地 checkout，再重新索引。stale 比较的是索引与本地基线，不是远端分支。控制面仍是单所有者/可信工程师实例；worktree 不等于 worker OS 沙箱，真实付费 SDK E2E 和隔离 worker 仍属后续阶段。
