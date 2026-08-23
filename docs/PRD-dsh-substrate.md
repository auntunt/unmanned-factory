# PRD：把 unmanned-factory 的执行底座换成 DeepSeek Harness

状态：草案 · 待决策
作者：与用户共同确认
日期：2026-08-21

## 0. 先纠正一个前提

你的原话是「基于 pi-agent / deepseek-harness 构建新底座」，隐含二选一。实测后这不是二选一：

`deepseek-harness`(下称 dsh)的 `packages/llm/llm-pi-ai/package.json` 里明确依赖
`@earendil-works/pi-ai@^0.82.1`。**pi-ai 是 dsh 的 provider 层底座**，dsh 是它的上层。
两者是叠的，不是并列的。

所以真正的选择是三档，不是两档：

| 选项 | 你拿到什么 | 你要自己写什么 |
|---|---|---|
| 只用 `pi-ai` | 统一的多家 provider 调用层(三种 wire 协议) | agent loop、工具、会话、权限、沙箱 —— 全部 |
| 用 `pi-agent-core` | 上面那些 + agent loop | 工具集、权限、会话持久化、沙箱 |
| 用 `dsh` | 上面全部 + 工具 + 权限模式 + 沙箱 + 会话 + 插件体系 | 只写适配层 |

本 PRD 主张第三档，理由见 §2。第一档留作降级预案(§9)。

## 1. 为什么现在动底座

当前 `ClaudeCodeHarness` 是唯一可用 worker，它带来三个已经付过学费的结构性问题：

**一、非流式导致的观测盲区。** `--output-format json` 全程不吐字节，跑完才一次性给
JSON。`Limits.stall_timeout_s` 的默认值只能是 `None` —— 这在 `harness/base.py:34-54`
里有整段血泪注释：2026-08-19 设成 120.0 之后三轮全被自己的停滞检测杀掉，
`wall_clock_ms` 是 120098/120097/120099，报成 `Bad file descriptor`，一路误导到
「启动失败」。**代价是我们现在对 worker 内部进展一无所知**，只能等它结束或超时。

**二、权限门是外挂的。** `permission/` 那套 PreToolUse hook 能用，但它建立在
claude CLI 的 hook 约定上。已知坑：hook 解释器不能用 `sys.executable`；脚本 stdout
接 `| tail` 会让子进程继承缓冲管道、握手超时、CLI 谎报 `Not logged in`；
`chmod 777`/`git reset --hard` 这类 canary 被 CLI 内置自拦，baseline 验不红，
只能自定义规则把 `touch xxx` 标 deny 才能端到端验证。**这些全是绕着别人的产品边界打补丁。**

**三、单点绑定。** 换模型只能在 Anthropic 家谱里换。routing.yaml 现在是
`A: [sonnet, opus, opus]` —— 三轮升级全在一家，一家挂了整条产线停。

## 2. 为什么是 dsh(经过核实的事实，不是宣传语)

以下每条都从一手源核实过，不是 README 转述。

**2.1 它有真正的 headless 契约。** 这是能不能用的生死线。
`.agents/notes/implemented/architecture/2026-08-09-headless-direct-core-entry-point.md`
原文定义 headless 产品契约为：

> one local task with final assistant text on stdout, a success-sensitive exit code,
> empty stderr on success, and no listening port

这跟 `HarnessAdapter.run()` 要的形状几乎一对一。而且它是**刻意做成无端口的** ——
headless profile 的依赖树里不含任何 `dsh-host-*`、ApiProxy、HTTP server、Web runtime、
browser client，并且有 config-dump 验收在 CI 里守着这条边界。这不是「顺便能 headless」，
是把它当契约在维护。

对比一下：`oh-my-pi` 没有 headless 模式，只给人在终端里用 —— 它不能当 worker。

**2.2 它有官方 Python SDK。** 我们是 Python 项目，这是决定性的。
`python/sdk/README.md`：`deepseek-harness-sdk`(import 名 `deepseek_harness`)，
通过 stdio 上的 newline-delimited JSON-RPC 驱动子进程。API 是同步的：

```py
from deepseek_harness import DeepSeekHarness

with DeepSeekHarness(provider="deepseek-official", model="deepseek-v4-flash",
                     max_tokens=49_152, cordis="path/to/cordis.yml") as harness:
    result = harness.run("Make the requested code change.")
```

`RunResult` 字段：`session_id / final_response / finish_reason / events /
notifications / session_root`。`finish_reason` 取值如 `completed` / `max-tokens` /
`error` —— 直接能映射到我们的 `ExitStatus`。

**注意一个语义陷阱**(README §45 明说)：`Session.run()` 拥有的是「从 prompt 进入
durable inbox 到下一次整体 idle」这段区间，`final_response` 是该区间内最后一条已提交的
根会话助手文本，**不是因果上属于该 prompt 的输出**。steering 和注入的 context 可能在
idle 前插进来。我们的适配层不能假设「一次 run 一个干净答案」。

**2.3 权限模式是配置层的，不是外挂 hook。**
`.agents/notes/implemented/feature/2026-08-15-product-subagent-noninteractive-permissions.md`：
权限模式由 Provider 的 Profile 级配置固定，**subagent 工具 schema 和
`SubagentStartRequest` 里都没有权限字段，所以模型或单次委派无法自己抬权**。
这正是我们 `permission/` 想达到但只能靠 hook 近似的效果。

它还带 `packages/hooks/hooks-claude-code` 和 `hooks-codex` —— 兼容既有 hook 协议，
我们现有权限规则有迁移路径，不是从零重写。

**2.4 它能反过来把 claude 当 subagent。**
`.agents/notes/implemented/feature/2026-08-04-claude-code-and-codex-subagent-backends.md`
表明 dsh 有 Claude Code 和 Codex 的 product provider。意味着迁移不是替换掉 claude，
而是**把 claude 降级成 dsh 下面的一个 provider** —— 现有能力不丢。

**2.5 工程质量信号。** MIT 许可、TypeScript、创建于 2026-08-13、今天(08-21)还在推、
未归档。`.agents/notes/` 下有 400+ 篇带 Problem/Decision/Verification/Alternatives
的架构决策记录，且分 implemented/proposed/rejected —— 包含 `rejected/` 才是真在用这套
流程，不是摆设。

## 3. 必须先讲清的风险

**3.1 developer preview，明确会破坏兼容。** README 第 11 行原文全大写：
`THERE WILL BE COMPATIBILITY-BREAKING CHANGES.` 仓库 8 天大。这是本方案最大的风险，
不是可以轻描淡写的注脚。缓解措施见 §5(适配层隔离)和 §8(版本钉死)。

**3.2 star 数 176995 但仓库只有 8 天。** 这个增速不符合正常项目曲线，
更可能反映 DeepSeek 品牌热度而非工程成熟度。**不要把 star 数当质量证据**，
上面 §2 的判断全部基于代码和架构笔记，不基于 star。

**3.3 文档只教 Web UI。** README 的 Run 一节只给 `npx @deepseek-ai/dsh web`。
headless 和 Python SDK 都要翻 `.agents/notes/` 和 `python/` 才找到。
说明这两条路是给开发者的，不是产品主路径 —— 出问题时社区答案会少。

**3.4 PyPI 上没有一个正式版。** 实测 `deepseek-harness-sdk` 的全部发布历史只有
4 个版本：`0.0.0.dev0`、`0.1.0rc6`、`0.1.0rc7`、`0.1.1rc1` —— **清一色 dev/rc，
最新就是 `0.1.1rc1`**。要求 `Python>=3.10`(我们 3.11/3.12 都满足)，
依赖 `deepseek-harness-runtime-bin==0.1.1rc1` 与 `pydantic>=2.12,<3`。

它对自己 runtime 用的是 `==` 精确钉死，说明上游也清楚版本漂移的危险，这对我们有利。

**pydantic 冲突已排除**(实测)：本项目 `pyproject.toml` 的运行期依赖只有
`SQLAlchemy==2.0.50` 和 `PyYAML==6.0.2`，dev 组只有 `pytest==9.0.3`，
当前 venv 里根本没装 pydantic。所以 `pydantic>=2.12` 是纯新增，不存在版本冲突。
`requires-python` 我们是 `>=3.12`，严于 SDK 的 `>=3.10`，也没问题。

**3.5 Node.js 运行时依赖。** dsh 是 TypeScript，Python SDK 靠 stdio 驱动一个
打包好的单文件 `dsh-jsonrpc-agent` 可执行文件(装 `deepseek-harness-sdk` 会带上
同版本的 `deepseek-harness-runtime-bin` 平台 wheel)。好消息是不需要我们自己管 pnpm
构建；坏消息是沙箱里要 ro-bind 这个 binary 所在目录 —— 跟现在 claude 的处境一样，
这条经验可直接复用。

## 4. 目标与非目标

### 目标

1. **多 harness 并存**，不是替换。`DshHarness` 与 `ClaudeCodeHarness` 同时可用，
   按任务类别或路由表选择。
2. **拿回流式观测。** 利用 `RunResult.notifications`(含 subagent 生命周期与会话事件，
   按 wire 顺序)让 `stall_timeout_s` 第一次可以安全开启。
3. **权限门内移。** 从外挂 hook 迁到 Provider 配置层，消灭「hook 解释器」「缓冲管道」
   这类与产品边界搏斗产生的故障模式。
4. **打破单一模型家族。** 阶梯可以跨家：`[deepseek-v4-flash, sonnet, opus]`。
5. **零回归。** 现有 77 后端 + 60 前端测试全绿，`ClaudeCodeHarness` 行为不变。

### 非目标

- 不重写编排层、监工层、审计层。`HarnessAdapter` 契约(`harness/base.py`)不动 ——
  它当初就是为「换 harness 只需再实现一次 Protocol」设计的，现在正好兑现。
- 不迁移 Web UI。dsh 自带 Web UI，我们不用它，看板仍是自己那套。
- 不追求把 claude 完全换掉。§2.4 说了它能当 dsh 的 subagent，但那是第三期的事。
- 不做多机分布式。

## 5. 接口设计:新增 `DshHarness`，一行不改现有契约

`harness/base.py` 的 `HarnessAdapter` Protocol 只要求一个 `run()`。所以本次改动
在 harness 层是**纯新增**：

```
factory/harness/
  base.py            # 不动
  claude_code.py     # 不动
  dsh.py             # 新增
```

### 5.1 字段映射(这是最需要评审的部分)

| `AttemptResult` 字段 | dsh 来源 | 风险 |
|---|---|---|
| `exit_status` | `finish_reason`: `completed`→OK，`error`→ERROR，`max-tokens`→ERROR | `None`(无 turn 结束)要单独判，README §45 说这可能发生 |
| `session_id` | `RunResult.session_id` | 直接可用 |
| `transcript_path` | `RunResult.session_root` 下的 JSONL | 需确认落盘路径与压缩(仓库有 zstd JSONL 决策) |
| `tool_calls` | 从 `events` 过滤工具调用事件 | 事件 taxonomy 要读 `microkernel-event-taxonomy` 笔记 |
| `tokens_in/out`、`cost_usd` | `packages/llm/token-meter` | **未核实取法**，见 §7 待验清单 |
| `diff`、`diff_hash`、`changed_paths` | **不从 dsh 取**，继续用现有 `worktree.py` 算 git diff | 有意为之：diff 是我们的事实来源，不依赖 harness 自述 |
| `stalled` | 由 notifications 静默时长判定 | 这次能真做，见目标 2 |
| `error_text` | stderr + `finish_reason=error` 的 durable code/message | headless 契约保证成功时 stderr 为空 |

`diff` 一栏是刻意的设计选择：**worker 自述改了什么不算证据，git 说改了什么才算。**
现在这么做的，迁移后不变。

### 5.2 模型阶梯的表达

`routing.yaml` 现在是裸模型名列表。跨家之后必须带 provider，否则 `deepseek-v4-flash`
和 `sonnet` 混在一个列表里无法解析。建议：

```yaml
by_class:
  A:
    - {harness: dsh, provider: deepseek-official, model: deepseek-v4-flash}
    - {harness: claude_code, model: sonnet}
    - {harness: claude_code, model: opus}
```

这是一次不兼容的格式变更，`routing.py` 与其 25 个测试要跟着改。
**保留旧格式解析**(裸字符串视作 `claude_code`)以免一次性大爆炸。

## 6. 分期

每期都以「能跑通并且测试全绿」为终点，不留半成品。

**第一期 · 打通(1 个 spike + 1 个适配器)**
先写 `spike/dsh_smoke.py`：装 SDK、指向 zjz 中转站或 DeepSeek 官方端点、
在一个临时 git 仓库里跑「改一个文件」的任务，把 `RunResult` 整个 dump 出来。
**这一步的产物是事实，不是代码** —— §7 待验清单靠它填。
然后写 `DshHarness.run()`，跑通单个真任务。

**第二期 · 对齐(测试 + 路由)**
`test_dsh_harness.py` 对齐 `ClaudeCodeHarness` 的现有测试矩阵(超时、停滞、
启动失败、空 diff)。`routing.yaml` 升级成带 provider 的结构。开 `stall_timeout_s`。

**第三期 · 权限内移**
把 `permission/rules.py` 的规则译到 dsh Provider 配置。**验收标准沿用已验证过的那条**：
`denied=1` 且探针文件未创建。已知 canary 陷阱(claude 内置自拦无害命令用 `touch xxx`
标 deny)在 dsh 上要重新摸一遍，不能假设相同。

**第四期(可选)· claude 降级为 subagent**
用 dsh 的 Claude Code product provider 统一入口。只有前三期稳定后才做。

## 7. 待验清单(必须先实测，不能写进代码里赌)

这些是我核实事实时**没能从静态源确认**的，第一期 spike 必须逐条落地：

1. `RunResult` 里 token 用量与成本的确切字段名和单位。README 没给，只说有
   `token-meter` 包。审计层要靠这个算钱。
2. `events` 的工具调用事件形状 —— `ToolCall.name / call_id / is_error` 怎么取。
3. `session_root` 下 transcript 的实际文件名、是否 zstd 压缩、能否直接喂给
   现有 `transcript.py`。
4. `finish_reason is None` 在什么情况下真的出现，以及此时该判 OK 还是 ERROR。
5. 中转站(zjz)兼容性:SDK 说继承 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`，
   但我们的中转站是 Anthropic/OpenAI 兼容形状。**这条可能直接决定方案可行性** ——
   若不兼容，要么挂 `llm-pi-ai` 自定义 provider，要么直连 DeepSeek 官方。
6. 单文件 runtime binary 在 bwrap 沙箱里的 ro-bind 路径。
7. `max_tokens` 与我们 `Limits` 的关系,以及 dsh 端有没有 wall-clock 超时。

## 8. 风险缓解

| 风险 | 缓解 |
|---|---|
| preview 破坏性变更(§3.1) | `requirements` 里**钉死精确版本**，不用 `^`。升级走单独 PR 并全量回归 |
| 适配层被上游变更冲穿 | 所有 dsh 特定逻辑关在 `dsh.py` 一个文件里，编排/监工/审计层零感知 |
| 新 harness 不稳定影响产线 | 灰度:先只对 B 类任务启用；`ClaudeCodeHarness` 始终是可回退默认 |
| 权限迁移出现豁口 | 第三期不合并除非 `denied=1` + 探针未创建端到端复现 |
| 中转站不兼容(§7.5) | 第一期 spike 就验这条，不通就走 §9 降级 |

## 9. 降级预案

若第一期 spike 表明 dsh 不可用(最可能的原因是 §7.5 中转站不兼容，或 preview
稳定性不够)，退到 **只用 `pi-ai`** 这一档：拿它的多 provider 调用层，
agent loop 用我们自己的。工作量大得多，但 `pi-ai` 依赖面小、语义简单，
而且它本来就是 dsh 的底座 —— 两条路的 provider 抽象是同一个，不浪费。

**不选 LangChain。** 抽象层厚、错误远离现场，跟我们「diff 是唯一事实」
的调试哲学冲突。

## 10. 需要你拍板的三件事

1. **模型阶梯要不要跨家？** 跨家是本方案的主要收益之一，但会引入
   `routing.yaml` 不兼容变更(§5.2)。也可以先纯 dsh 内部换模型，格式不动。
2. **第一期用哪个端点？** zjz 中转站(未知是否兼容)还是 DeepSeek 官方
   API(要新 key，但能立刻验证方案本身)。我倾向官方 —— 先把 dsh 本身验通，
   再单独解中转站的兼容问题，别让两个未知量互相污染。
3. **preview 风险你能接受到什么程度？** 若不能接受 8 天大的仓库进产线，
   现在就该走 §9，不必做第一期。

