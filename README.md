# webuddy

webuddy 是可定制的 AI 工作伙伴：先创建工作区，再用自然语言描述目标，由职能体完成规划、执行、验证与成果交付。通过 Skill 和维护对话持续积累专门能力。

最新增量：[持续托管编码](docs/continuous-coding.md)：普通自主任务默认一个工作区和会话，保留六环展示，连接恢复与验收修复直接接续。[项目 ZIP 导入和反馈接续](docs/implementation-2026-09-10.md)继续保留。

运行时挂载：[架构核对与优化](docs/runtime-mounting-review-2026-09-12.md)说明职能体、方法模块、数据源与工具的边界；[资料接入说明](docs/reference-source-operations.md)提供 CLI／MCP 文本资源导入、项目槽位绑定和 Claude 按需读取路径。

主动验收：[独立验证现场与逐项证据](docs/active-verification-2026-09-12.md)说明验证工具、源码保护、证据补齐和看板展示。

## 自主交付工作台（v3）

新增自主运行策略、持久任务队列、模型分级与失败升级、调用记录、Agent 能力版本库、自动交付归档和全量日志导出。工作台包含总览、运行看板、项目、Agent 能力库、模型调用记录；保留 v2 项目知识、SDK 和 GitHub 交付接口。

- [顶层架构与对象边界](docs/v3/ARCHITECTURE.md)
- [关键设计决策](docs/v3/DECISIONS.md)
- [页面与用户流程](docs/v3/USER-JOURNEYS.md)
- [启动、迁移与运行说明](docs/v3/RUNBOOK.md)
- [验证记录与当前边界](docs/v3/VALIDATION.md)

本地体验：安装依赖并构建前端后，运行 `.venv/bin/python scripts/preview_v3.py`，打开 `http://127.0.0.1:8790`，使用 `preview / factory-preview-only`。这是明确标记的隔离演练：执行真实的 Git 工作区和项目检查，模型响应由脚本提供，不调用模型或外部服务。真实工作使用下方 `factory-web` 入口和独立数据目录。

Token 计费和额度由中转站统一管理，webuddy 不再因本地费用记录或配额中断任务。详见 [计费边界](docs/gateway-billing.md)。

## 工程 Harness 重写（v2）

新增带登录的工程工作台：需求梳理与任务图、分档模型调度、Claude/Codex/DSH 适配、独立工作区验证、可追溯问答、GitHub Issue 分流和 PR 发布。现有 CLI 与历史记录保留。

- [设计规划与同类项目分析](docs/rewrite/PLAN.zh-CN.md)
- [全新工作台与运行环境](docs/rewrite/WORKBENCH.zh-CN.md)
- [运行说明](docs/rewrite/RUNBOOK.zh-CN.md)
- [现有服务器更新与 Hermes 交接](deploy/HERMES-HANDOFF.md)
- [Project Agent 实践说明](docs/rewrite/PROJECT-AGENT.zh-CN.md)
- [已完成范围、验证结果与后续里程碑](docs/rewrite/STATUS.md)

新入口为 `.venv/bin/factory-web serve`；先用 `uv sync --frozen --all-extras` 安装 SDK 与配套运行时，按运行说明构建前端并创建账号。运行配置支持保存模型分工、连接测试和执行限制。维护此虚拟环境时持续保留 extras，避免普通 `uv sync` / `uv run` 将其卸载。下方是原有引擎文档，旧 `factory api` 入口不应直接暴露公网。

一个调度 + 审计层：把任务派给 coding agent，用四道监工判收，全程留可回查的证据。

设计文档是 `docs/superpowers/plans/2026-08-06-unmanned-factory-p0.md`（约 4700 行）。
那是**建造日志**不是手册 —— 按时间顺序记着每个非显然决定和它的理由，包括
后来被证伪的判断。想知道「为什么是这样」去那儿；想知道「怎么跑」看下面。

## 30 秒跑一个任务

```bash
uv sync
uv run factory run examples/greet_task.yaml --workspace ~/some-repo --db audit.db
```

**单个任务时 `run` 不开 worktree**（多任务会自动开），此时 workspace 就是那个
仓库本身，改动留在工作区但不会被提交（见「三条边界」第 1 条）。要产出落在
任务分支上就显式加 `--worktree`：

```bash
uv run factory run examples/greet_task.yaml --workspace ~/some-repo \
  --db audit.db --worktree
```

## 无人跑批

```bash
# 入队（一个失败则一个都不入队）
uv run factory queue --queue ~/.factory/q tasks/*.yaml

# 抽干就退（loop 默认开 worktree，要关得显式 --no-worktree）
uv run factory loop --queue ~/.factory/q --workspace ~/repo --db audit.db \
  --idle drain --budget-usd 5 --max-runtime 14400

# 昨晚跑得怎么样
uv run factory queue --queue ~/.factory/q --history 50
```

两个上限都要给。`--budget-usd` 单独不够：**超时的 attempt 记 $0**（被 kill 的
进程不打 usage payload），所以反复超时的任务在预算眼里是免费的 —— 墙钟是超时
躲不过的那道闸。两个都没给时 `loop` 会各打一条警告。

第三道闸默认就开着：`--max-unpriced-streak 2`，连续 2 个任务的花费记不上账就
停机。它数的不是钱，是「有多少次派发的价格是假的」—— 连续两次说明坏的是环境
（CLI 挂了、网断了），不是任务。单次超时不触发，因为那是设计里的正常出口。
停机后 `queue --history` 里那几条会写明原因，队列里剩下的任务原地不动。

`--timeout` 到点时杀掉的是**整个进程组**，不只是 claude 自己。它是个 node
进程，会拉起 MCP server 和 Bash 工具的每条命令；只杀父进程的话，那些会在
循环停机之后继续跑（实测一次 3 任务的 drain 留下 12 个孤儿）。先 TERM 等 3s
再 KILL —— 那 3 秒是留给 CLI 把 transcript 落盘的，漏账时那是唯一的线索。

工厂里每一条会超时的外部调用都走这条路，不只是派发：回归监工跑的 check、
三个模型监工、入口的提取和 check 探针、口述转录、runbook 的 `requires` 探针。
凡是「会拉起我们不认识的东西」的命令都算，纯 `git` 调用不算。

定时启动用 `examples/launchd/com.factory.loop.plist`，**别照抄上面那条命令** ——
launchd 的 PATH 里没有 nvm 装的 `claude`，夜跑会每次派发都失败。plist 里标了
「←」的行按本机改，改完跑 `plutil -lint`。

## 控制室（给人看的那一屏）

```
uv run factory api --db audit.db --queue ~/.factory/q     # 后端，含 SSE
cd frontend && npm run dev                                 # 前端：新版工程工作台
```

浏览器开 `http://localhost:5173/` 看 live；开
`http://localhost:5173/?replay=T-142&speed=4` 按 4 倍速回放一个跑过的任务。
两种模式前端**不区分**：`GET /api/events` 发的是同一种事件
（形状见 `factory/events.py` 模块头），只是 replay 从 audit.db 展开，
live 从「两次快照的差」推出来。

回放的保险：`factory replay T-142 --db audit.db --speed 0 > rec.jsonl` 把
整条时间线落盘，不依赖 demo 那台机器上的库和 transcript 目录还在。

live 模式里**正在跑**的那一轮，现场文本来自 `~/.claude/projects` 下
mtime 最新的 session 文件 —— 是猜的（transcript 路径要跑完才落库）。
猜错只影响屏幕，不进审计。走 Caddy 时按 `deploy/Caddyfile.example`
关掉 `/api/events` 的缓冲，否则现场行会一批批跳。

## 子命令

| 命令 | 干什么 |
|---|---|
| `prd` | 录音 / 自由文本 → 任务 YAML 草稿，过入口闸门 |
| `run` | 派发一个或多个任务 |
| `queue` | 入队 / 看状态 / 看历史 |
| `loop` | 跑批：不断认领队列里的任务并派发 |
| `show` | 打印一个任务的完整审计轨迹 |
| `override` | 人工定案 resolution（事后回填） |
| `defect` | 事后挂 defect 捕获漏报，支持 `--commit <sha>` 反查 |
| `metrics` | 监工命中率 / 漏报 / 单位命中成本 |

## 四道监工

两个确定性的（零成本）、两个调模型的：

- **regression** — 跑任务声明的 check。没有可执行 check 就判 FAIL（fail-closed）
- **scope** — 改动是否超出 `declared_paths`
- **spec** — 逐条核 diff 是否满足验收标准，不给 build log（拿不到过程叙述）
- **architecture** — 出**意见**，不能一票否决合并

三轮不过升级给人（`max_rounds`，默认 3）。分级引擎把任务分 A/B/C/D：A/B 走无人，
C 和 D 都在**派发之前**停下，adapter 一次都不会被调用 —— 但两者不是一回事：

- **D**（不可逆，如 `prod_deploy`）→ `blocked_hard_gate`，agent 只能生成待执行脚本
- **C**（无廉价裁判，如 `*auth/*`）→ `escalated`，交给人

混成一句「C/D 被硬闸门拦住」会让人以为 C 也不可协商，而 C 只是这一层不该自动判收。

## 验收标准怎么给

规格监工核的是**正文**，所以任务必须给出正文。两种合法形状：

```yaml
# 形状一：标准写在任务里（口述需求的形状，examples/greet_task.yaml）
acceptance:
  - greet(name) 返回 str，内容是 "Hello, {name}!"

# 形状二：标准在 PRD 里，任务只引编号（examples/spec_doc_task.yaml）
spec_ref: [AC-1, AC-2]
spec_doc: PRD.md          # 相对 workspace 根
```

**`spec_ref` 和 `spec_doc` 必须成对。** 只写编号的话，规格监工收到的字面就是
`AC-1` 这四个字符 —— 它核不了任何 diff。这种任务在派发之前就被拦：入口闸门
记 `[dangling-spec-ref]`，`factory run` 记 `pre-dispatch-spec-ref`，
两条路都是 adapter 一次不调、一分钱不花。

拦它而不是放它进去，是因为放进去的那条路每一步都「正常工作」：监工判 fail
（合理），claim 不带 `supervisor-` 前缀（它是真实发现，不是监工故障），于是
被当成真问题打回 worker（按设计），而 worker 改不了「AC-1 没有正文」——
三轮烧完升级给人。全链路没有一个组件出错。

编号在文档里认三种行首形状（`- AC-1: …`、`### AC-1 …`、`AC-1. …`），
缩进续行会被合并，认不出就报「查不到」。**确定性解析，不让模型抽** ——
抽错的标准比没有标准更危险：监工会拿着一条不存在的要求判 diff，判得理直气壮，
而没有任何下游能发现那条标准是编出来的。

## 三条边界（改代码前先读）

1. **只在 linked worktree 里提交。** 单任务 `run` 不开 worktree（`loop` 默认开），
   此时 workspace 就是人的仓库本身，所以这条拒绝路径**默认会走到** ——
   人的检出目录里冒出一个没人要求过的 commit 是这一层能造成的最坏后果。
2. **只提交监工审过的那一组文件**（`git add -- <paths>`，不是 `add -A`）。
   check 命令自己会造 `__pycache__`，`add -A` 会让 `commit` 和 `diff_hash`
   描述不同的内容。
3. **落地失败不改判决。** 一个全绿的任务不该因为 `user.email` 没配变成
   escalated。

## 开发

```bash
uv run pytest -m "not smoke"   # 离线，不需要 API key（当前 577 个）
uv run pytest -m smoke -s      # 真调 claude，约 6 分钟
```

smoke 的花费实测在 $0.5 到 $2.5 之间浮动：一轮过是前者，第一轮超时后
换模型重跑是后者。别把它当固定成本。

`tests/__init__.py` 必须存在：site-packages 里有第三方装的顶层 `tests` 包，
没有它本地测试会 `ModuleNotFoundError`。

审计库里不许有明文密码或 key（`factory/redact.py` 管这个）。
