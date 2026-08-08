# 自动化无人工厂

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
