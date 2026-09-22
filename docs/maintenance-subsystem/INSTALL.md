# webuddy 运维维护子系统 · 安装与运行

版本：`0.1.0`（见 `factory.control.maintenance_subsystem.VERSION`）
契约版本：`maintenance-subsystem/1`（见 [CONTRACT.md](CONTRACT.md)）

CLI、HTTP 路由、页面用的是同一个业务核心（`MaintenanceSubsystem`），本文只讲
命令行怎么装、怎么接、怎么跑，业务对象与状态机见 CONTRACT.md。

## 依赖

- Python >= 3.12
- git（代码库登记、探测、导出补丁都依赖本机 `git`）
- `uv`（推荐）或 `pip`
- **执行器**：真正跑模型、改代码、跑检查的是已安装并登录的 Claude Code 或
  Codex 运行时，不是本子系统自带的东西。子系统只负责入队、记录、导出。
  用 `factory-runtime doctor` 检查当前主机上的运行时是否已就绪（是否装了对应
  SDK/CLI、是否已登录）——这个命令不发起任何真实模型调用，可以随时跑。

## 安装

```bash
# 推荐：uv
uv sync

# 或 pip（可编辑安装）
pip install -e .

# 需要用 Claude Code 作为执行器
uv sync --extra claude          # 或 pip install -e ".[claude]"

# 需要用 Codex 作为执行器
uv sync --extra codex           # 或 pip install -e ".[codex]"
```

安装后得到 `webuddy-maintenance` 命令（`pyproject.toml` 的
`[project.scripts]`）。

## 数据目录与配置

CLI 与 webuddy 网页服务共用同一套环境变量语义：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `FACTORY_CONTROL_DATA` | `control.db` 所在目录 | `~/.factory/control` |
| `FACTORY_WORKSPACE_ROOT` | 已登记代码库必须位于其下的目录 | `~/projects` |
| `FACTORY_TIMEZONE` | 监控总览「今天」窗口的时区 | 本机时区 |
| `FACTORY_EMBED_ORIGINS` | 允许 iframe 嵌入 `/embed/*` 的来源 | 空 |
| `FACTORY_TASK_TIMEOUT` | 单次执行最长秒数 | `14400` |
| `FACTORY_CLONE_TIMEOUT` | 登记 git URL 时后台 clone 的超时 | `600` |
| `FACTORY_*_PROVIDER` / `FACTORY_*_MODEL` | planner/cheap/standard/strong 四个角色的模型配置 | `codex` / 空 |

完整清单见 [config.example.env](config.example.env)（占位符，无密钥）。

CLI 也可以用 `--data-dir DIR`（等价于 `FACTORY_CONTROL_DATA=DIR`）和
`--workspace-root DIR` 显式覆盖，或用旧式 `--db PATH` 直接指定
`control.db` 文件路径（向后兼容，不建议新用法再用）。

## 两种接法

### A. 连接现有 webuddy 服务的数据域

CLI 指向与正在运行的 webuddy 网页服务**相同**的 `FACTORY_CONTROL_DATA`
（同一个 `control.db`），就能看到同一批代码库、需求、任务。网页服务持有
执行器锁并真正执行队列；CLI 只负责入队（`submit`/`dispatch`/`approve`/
`answer`/`feedback`）与查询（`repo`/`requirements`/`overview`/`graph`/
`events`/`actions`），从不与网页服务争抢执行器锁。

```bash
export FACTORY_CONTROL_DATA=~/.factory/control   # 与网页服务相同
webuddy-maintenance repo list
webuddy-maintenance submit --project <project_id> --text "登录页偶发白屏"
webuddy-maintenance events <task_id> --follow
```

### B. 独立最小运行时（不启动网页服务）

在没有网页服务的机器/容器上，`webuddy-maintenance runtime` 单独持有执行器
锁并真实执行队列——它是一个精简到只跑调度、不含网页/HTTP 层的常驻进程。

```bash
# 终端 1：初始化数据域，登记一个代码库
webuddy-maintenance init
webuddy-maintenance repo add --source /path/to/repo --name my-service
webuddy-maintenance repo adopt-checks <project_id> pytest

# 终端 2：常驻运行时，持有执行器锁
webuddy-maintenance runtime

# 终端 1：提交需求、跟踪、回答、批准
webuddy-maintenance submit --project <project_id> --text "登录页偶发白屏"
webuddy-maintenance events <task_id> --follow
webuddy-maintenance answer <task_id> --text "只在跨月区间复现"
webuddy-maintenance approve <task_id>
webuddy-maintenance export <task_id> --output ./out
```

`runtime` 与 CLI 的其它子命令可以在不同终端、不同进程里对同一个
`control.db` 并发运行——它们是同一套 `Service`/`DurableQueue` 机制，
一个持锁执行，其余只入队和只读。

## 启动 / 停止

- `runtime` 前台运行；`Ctrl-C`（SIGINT）或 `kill`（SIGTERM）触发优雅停机：
  停止调度、等待在跑的任务收尾、释放执行器锁，打印
  `{"runtime": "stopped"}` 后退出。
- 执行器锁是一个文件锁（`<control.db 同目录>/<stem>.worker.lock` 上的
  `flock`），进程异常退出时操作系统自动释放，不需要手工清理；`runtime`
  或网页服务下一次启动会重新获得它。
- `webuddy-maintenance runtime --once-idle-after SECONDS` 是给脚本化验收用
  的：队列连续 `SECONDS` 秒既无 pending 也无 running 任务后自动退出，
  不需要人守着按 Ctrl-C。

## 事件与产物规范

- `events`/`events --follow` 返回的每条事件是
  `{"sequence", "kind", "payload", "at"}`，与网页任务现场读的是同一份
  `runs` 表下的事件流。`--follow` 以 NDJSON（每行一个 JSON 对象）持续输出，
  直到任务到达 `delivered`/`failed`/`cancelled` 或收到 Ctrl-C。
- `export` 写到磁盘上的是**补丁**（`git format-patch` 产物），不是部署产物；
  子系统从不触碰目标环境。回执（`receipt`）里的 `diff_hash` 与写出的补丁
  字节做 SHA-256 校验对应，可独立验证。

## 明确不做

- **不自动部署**：交付形态永远是补丁包 + 检查结果，从不推送到任何生产/预发
  环境。
- **不跨安装同步**：两个 `FACTORY_CONTROL_DATA` 不是同一个数据域就是完全
  独立的两套记录，子系统不做任何跨库同步或代理转发。
- **不支持原地暂停**：执行器没有暂停能力，`actions` 永远不会包含 `pause`；
  需要停下来就是 `cancel`（不可逆）或让它自然停在人工确认点（回答/批准/
  补充信息）。

## 嵌入到其他系统（iframe）· 部署边界

- 入口 `/embed/maintenance/*`：同一组页面，不带 webuddy 外框；页内所有链接都留在嵌入前缀下，不会跳进主壳。
- 设置 `FACTORY_EMBED_ORIGINS="https://host.example.com"`（空格分隔；仅 https 或本机 http），只有 `/embed/*`
  改发 `Content-Security-Policy: frame-ancestors <这些来源>`，其余页面与 API 仍是 `X-Frame-Options: DENY`。
- 会话 cookie 是 `SameSite=Strict`，本期**不放宽**，也不建设跨站 SSO。因此：
  - **支持**：宿主与 webuddy **同站**（同一可注册域，如 `host.example.com` 嵌 `webuddy.example.com`；
    或同一域名下的反代路径）。用户在 webuddy 已登录即可在宿主页里直接使用。
  - **不支持**：宿主与 webuddy **跨站**（不同可注册域，或 `localhost` 对 `127.0.0.1`）。浏览器不会在
    iframe 里携带 Strict 会话 cookie，嵌入页只会显示登录页；需要跨站嵌入时应走后续的 SSO 设计，而不是放宽
    cookie/CSRF/CORS。
- 写操作仍要求 Origin 与 `FACTORY_PUBLIC_ORIGIN` 一致并带 CSRF 头；iframe 内的页面本身来自 webuddy 源，天然满足。

## 已验证 / 未验证（2026-09-23，集成方实测）

已用真实执行器验证（Claude Code 运行时，模型 `claude-sonnet-5`，走本机用户自己的
`~/.claude/settings.json` 中转配置；验收进程用 `env -i` 启动，不继承宿主会话变量）：

- **接法 B 独立最小运行时**：从分支全新 clone → `uv sync --extra claude` → `init` →
  `repo add` → `repo adopt-checks` → `submit`（回执如实报执行器离线）→ `runtime` →
  模型提出澄清问题 → `answer` → `approve` → 交付并通过 pytest → `export` 写出补丁。
  合成仓库，单任务花费 $2.55。补丁见 `evidence/standalone-runtime-delivery.patch`。
- **接法 A 连接网页服务数据域**：网页服务持有执行器，CLI 在同一 `FACTORY_CONTROL_DATA`
  上 `approve`、`events --follow`、`show`、`export`，与页面读到同一任务对象。

仅由自动化测试（注入 fake 执行器）覆盖：URL 克隆登记（`repo add --source https://…`）、
`intake-sources` 吊销、插件停用后拒绝接入。

嵌入（2026-09-23 实测，截图 `evidence/10-*`、`11-*`）：`127.0.0.1:8800` 的宿主页 iframe 打开
`127.0.0.1:8799/embed/maintenance`（同站、不同端口），沿用已有会话看到监控，点「进入现场」下钻、点「返回」
回到监控，全程停留在嵌入前缀内；同一页面换成 `localhost:8800`（跨站）宿主时 iframe 只显示登录页。
跨站 iframe 内登录后能否保持会话未实测（验收不在页面输入密码；按 Strict 语义预期不能）。

未验证：Codex 执行器；非 macOS 主机；真实生产域名下的同站子域嵌入。
