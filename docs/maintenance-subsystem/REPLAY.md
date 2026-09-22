# 本地验收现场 · 重放交接

候选：`codex/maintenance-subsystem` @ `7b6d684`（代码工作树 `/Users/auntlee/workspace/.factory-worktrees/operations-20260921`）。
本文只列现场位置与启动方法，**不含任何密码、令牌或会话内容**；数据全部为合成验收数据，未清理、未改写。

> 现场都在 `/private/tmp` 下的会话临时目录里，系统重启或临时目录清理后可能消失。下文用
> `S=/private/tmp/claude-501/-Users-auntlee-workspace--factory-worktrees-operations-20260921/4b0b0031-3904-492b-842b-fa51a864f2c0/scratchpad`。

## 1. 网页服务数据域（合成验收：人工 + 机器提交、监控、画布、反馈）

| 内容 | 位置 |
|---|---|
| 数据目录（`FACTORY_CONTROL_DATA`） | `$S/accept/data/`（`control.db`、`users.db`、`control.worker.lock`、`deliverables/`） |
| 工作区根（`FACTORY_WORKSPACE_ROOT`） | `$S/accept/ws/` |
| 合成代码库 | `$S/accept/ws/demo-invoice`（`local/demo-invoice`，project_id `adfe445fd5204f87972ee529831d4c0d`，已声明合成） |
| 执行工作副本 | `$S/accept/ws/.factory-1dde3d7db0b347bc8bf2bb16c7f1845b-flsrvz83`（v2）、`$S/accept/ws/.factory-42a41acf52cb4e21a52156273f1aafd8-8yp0nc3k`（v3） |
| 环境脚本（无凭据） | `$S/accept/env.sh`：清掉宿主会话变量，设数据目录/工作区/`FACTORY_PUBLIC_ORIGIN=http://127.0.0.1:8799`/时区/模型 profile |
| 服务日志 | `$S/accept/web.log` |

任务（均在该数据域）：
- `67a63c885a894e2fa5478760283ef748` v1 人工需求，规划前被脏工作区拦下 → 已取消
- `8b308f824d5542b882cb8125615e19ac` v2 反馈重开，已交付（执行 `1dde3d7d…`）
- `525b661e4e8d4228b99c826f3fcc4142` v3 交付后反馈，已交付（执行 `42a41acf…`；此轮从旧基线重做，是初审指出的历史错误，保留原样）
- `ec368737eb07448bbfbc2f2a14cefd12` API 机器需求，停在「待批准」，作为待处理样例保留

**含敏感内容、只给路径不要外传**：`$S/accept/source.json`（接入来源创建响应，含一次性接入令牌）、
`$S/accept/cookies.txt` 与 `$S/accept/sess.txt`（验收登录会话）。

## 2. 独立最小运行时数据域（干净 clone，无网页服务）

| 内容 | 位置 |
|---|---|
| 干净 clone（当时的分支提交 `19a3f2d`，独立 `.venv`） | `$S/clean/app` |
| 数据目录 | `$S/clean/data/` |
| 工作区根 / 合成代码库 | `$S/clean/ws/`，`$S/clean/ws/demo-units`（project_id `697d4b045f0c4301b286449424787b45`） |
| 执行工作副本 | `$S/clean/ws/.factory-a019fad7bf5f4a77b0babe1fd953687a-r2-ifkoehwi` |
| 环境脚本（无凭据） | `$S/clean/env.sh` |
| 运行时日志 / 事件 / 回执 | `$S/clean/runtime.log`、`$S/clean/events.ndjson`、`$S/clean/submit.json` |
| 导出补丁 | `$S/clean/out/maintenance-a019fad7bf5f4a77b0babe1fd953687a.patch` |

任务 `ad40abd9976245f3a7c8364f0fe1400f`：模型澄清 → 回答 → 批准 → 已交付。

## 3. 启动命令（只读验收用）

8799 网页服务（验收数据域 + 允许 8800 宿主框入 `/embed/*`）：
```bash
cd /Users/auntlee/workspace/.factory-worktrees/operations-20260921
S=/private/tmp/claude-501/-Users-auntlee-workspace--factory-worktrees-operations-20260921/4b0b0031-3904-492b-842b-fa51a864f2c0/scratchpad
( . $S/accept/env.sh; FACTORY_EMBED_ORIGINS="http://127.0.0.1:8800 http://localhost:8800" uv run factory-web serve --port 8799 )
```
前端产物来自该工作树的 `frontend/dist`（已按 `7b6d684` 构建）；如需重建：`cd frontend && npm run build`。

8800 宿主页（纯静态，无凭据）：
```bash
cd $S/embed-host && python3 -m http.server 8800 --bind 127.0.0.1
```
- 同站：`http://127.0.0.1:8800/`（`index.html`，iframe 指向 `http://127.0.0.1:8799/embed/maintenance`）
- 跨站对照：`http://localhost:8800/cross.html`（预期只显示登录页）

登录：验收账号的密码不在任何文档里。请在同一数据域自建账号（交互式输入密码，不落文档）：
```bash
( . $S/accept/env.sh; uv run factory-web create-user <你的用户名> )
```
同站 iframe 与 8799 主站共用 `127.0.0.1` 的会话 cookie，先在 `http://127.0.0.1:8799/` 登录再开宿主页即可。

CLI 只读查询（同一数据域，与页面读同一对象）：
```bash
( . $S/accept/env.sh; uv run webuddy-maintenance overview )
( . $S/accept/env.sh; uv run webuddy-maintenance show 8b308f824d5542b882cb8125615e19ac )
( . $S/accept/env.sh; uv run webuddy-maintenance events 525b661e4e8d4228b99c826f3fcc4142 )
( . $S/clean/env.sh; cd $S/clean/app && uv run webuddy-maintenance show ad40abd9976245f3a7c8364f0fe1400f )
```
独立运行时（如需确认进程形态；队列当前为空，不会触发模型）：
```bash
( . $S/clean/env.sh; cd $S/clean/app && uv run webuddy-maintenance runtime --once-idle-after 10 )
```

**注意（会产生付费调用的动作）**：8799 服务启动即持有执行器锁，`env.sh` 把模型配置为用户本机
`~/.claude/settings.json` 的中转（claude / claude-sonnet-5）。只读验收时不要在页面或 CLI 上批准
`ec368737…`、回答问题或提交新需求/反馈，否则会真实调用模型。`env.sh` 用 `unset` 清掉当前 shell
的 `CLAUDE_CODE_*`/`ANTHROPIC_*` 变量，以免借用宿主会话凭据。

## 4. 已归档证据（仓库内，随提交保留）

`docs/maintenance-subsystem/evidence/`：
- 截图 `01`–`11`（监控、画布、任务现场、代码库、需求接入、390 宽、分组布局、同站嵌入、跨站对照）
- `key-events.json`：四个验收任务的关键事件（已裁剪，无凭据）
- `machine-intake-receipts.txt`：机器接入回执（201 → 重复 200）
- `v2-delivery.patch`、`v3-delivery.patch`、`standalone-runtime-delivery.patch`
- `README.md`：历史错误说明（v3 未保留 v2 用例、当时 synthetic 为 false、旧画布布局）

临时目录里的原始事件流：`$S/accept/events-rev2.ndjson`、`$S/accept/events-rev3.ndjson`、`$S/clean/events.ndjson`。
