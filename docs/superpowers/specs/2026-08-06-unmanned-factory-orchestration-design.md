# webuddy：Agent 调度 + 监工审计层 — 设计文档

日期：2026-08-06
状态：待评审

---

## 1. 目标与定位

在现有 coding agent（Claude Code / Codex CLI / qwen-code 等）之上加一层调度与审计，使
「录音 → 规格 → 无人构建 → 交付」成为可重复的流程。

**核心价值不是「永远用最强的 agent」**，而是**换 agent 的成本足够低**。外部榜单滞后、且不评测
harness 本身（只评模型或自制 scaffold），"最新=最强"今天机器不可判定。真正值得投工程量的是
抽象边界干净——换 harness 是改一行配置，而不是重写业务逻辑。

**已确认的既有事实**：使用者的日常部署工作已经无人化，靠手写 runbook（如
`smy-image-batch-studio` 打包部署流程）驱动 agent 完成构建、上线、验证。本设计不是从零建立
自动化能力，而是把**已被验证可行的做法系统化、使其可复利、并加上强制执行机制**。

---

## 2. 已锁定决策

| # | 决策 | 选择 |
|---|---|---|
| 1 | 使用范围 | 自己 / 小团队内部工具。不做多租户、计费、对外合规报告 |
| 2 | 人工闸门 | 三道（规格确认 / 技术方案评审 / 交付验收）+ 破坏性操作硬闸门 |
| 3 | agent 选型 | 手工维护路由表（外部榜单 + 发布时间），不自建 eval harness |
| 4 | 首个切口 | 完整商业项目 PRD → 上线 |
| 5 | 监工配置 | 四个监工全开跑两周，再按命中率裁剪 |

决策 3 的后果需明确接受：价值主张是「换得快」，不是「选最强」。
决策 4 与现有证据（METR 时间跨度趋势线）冲突，第 5 节给出结构性处理方案，不依赖运气。

---

## 3. 核心设计：按裁判成本分级，而非按项目分级

### 3.1 问题

一个完整商业项目里混着两类活：裁判便宜的（CRUD、数据层、表单、迁移脚本、测试补全）和裁判昂贵的
（认证鉴权、支付、并发/事务边界、新 UX、跨模块架构决策）。把整个项目当一个「无人任务」下发，
等于让第二类跟着第一类一起无人化。这是 Replit 删库事故与 GitClear 代码退化的同一个根因。

### 3.2 分级判据（四性质）

一个任务可以无人，当且仅当它同时满足：

1. **裁判便宜且客观** — 存在可执行、agent 无法伪造的验收命令
2. **幂等** — 重跑一次无副作用
3. **可逆** — 不含不可逆状态转移
4. **失败知识已编码** — 已知陷阱已变成检查项

`smy-image-batch-studio` 部署 runbook 是四性质齐备的实例：

- 裁判：`curl -sf /health`、四个 HTTP 200，以及
  `docker inspect smy-studio-api-1 --format '{{.Image}}'` 对比
  `docker images smy-studio-api:latest --format '{{.ID}}'`
  —— 后者是关键，agent 自称"已部署"是申报，**镜像 ID 对得上才是证据**，且无法伪造
- 幂等/可逆：volume 持久化（`smy_storage` / `smy_db`），换镜像不丢数据，无 Alembic
- 已编码陷阱：8011 被驭流占用、`--load` 不加则镜像不落 daemon、`docker restart`
  会静默继续跑旧镜像、`rm -f .env` 否则前端把 localhost 编进产物、dockerproxy.net 抖动绕法

**结论：不存在"部署要不要人卡"这个问题。** 满足四性质的部署是 A 类；带 Alembic 迁移、无 volume
隔离的部署是 D 类——因为它不可逆，不是因为它叫"部署"。该 runbook 自身已划出这条线：
*"若改了已存在表的列，需手动处理"*。

### 3.3 四个等级

| 等级 | 判据 | 走法 |
|---|---|---|
| **A** 机器可判 | 四性质齐备 | 全自动。人只看最终报告 |
| **B** 可判但影响面大 | 能测，但触及跨模块契约 / 公共接口 | 自动执行，产出须过对抗性 review 才进合并队列 |
| **C** 无廉价裁判 | 认证 / 鉴权 / 加密 / 密钥 / 支付 / 事务边界 / 新 UX | 不无人。agent 只出方案和草稿，人工审阅后落地 |
| **D** 不可逆 | schema 迁移、数据删除、生产部署、force-push | 硬闸门，非旁路。agent 只能生成待执行脚本，不允许提交 |

### 3.4 分级必须由静态规则判定

C / D 的判定用**路径匹配 + 关键词 + AST 特征**，不接受 agent 自我申报。触发升级的示例：
`auth/`、`payment/`、`migrations/`、`Dockerfile`、CI 配置、密钥相关 API 调用。

理由：让 AI 判断「这件事该不该让 AI 自主做」是循环论证，而这恰好是失败代价最大的位置。

**已知代价**：规则会有漏判（某任务被标 A 但实为 C）。因此审计层必须能事后回答
「这个改动当时走哪条路、凭什么规则」，使漏判可被发现与修正。这是审计成为承重墙而非装饰的原因。

---

## 4. 监工层

### 4.1 设计前提：监工要有牙，需独立证据而非独立意见

同模型、同上下文、读 worker 自己的总结做 review，结果是盖章通过。因此四个监工刻意设计成
**三个产证据、一个出意见**：

| 监工 | 输入 | 产出 | 独立性来源 |
|---|---|---|---|
| **回归监工** | runbook 验证命令 | 逐条 pass/fail | 纯执行零判断，自己跑 curl 和镜像 ID 比对 |
| **规格监工** | spec + diff（**不给** build log） | 每条验收标准 pass/fail + 证据 | 拿不到过程叙述，只能对代码和标准核 |
| **风险监工** | diff 路径 + AST | 是否触及 C/D 类 | 静态规则优先，不接受自我申报 |
| **架构监工** | diff + 周边代码，干净上下文 | 约定违背、重复实现、死代码 | 干净上下文即独立性 |

架构监工是唯一「出意见」的角色，负责测试测不出的东西——GitClear 记录的 churn / 重复度上升
只有这类 review 能拦。

### 4.2 工头（dispatcher）

收监工裁决后：

- 全绿 → 进合并队列
- 有红 → 带**具体失败项**打回 worker，**最多 3 轮**
- 3 轮不过 → 升级给人

retry 上限是必需的，否则是无限烧 token。

### 4.3 为什么这个扇出是安全的

Cognition《Don't Build Multi-Agents》警告的是多 agent **干活**（交接丢上下文、错误累积）；
Anthropic 的结论是多 agent 在**可并行、受上下文窗口限制**的子任务上划算。审查完全符合后者——
每个监工只需 diff + spec，不需要整个构建历史。所以监工扇出位于该分界线的安全侧。

---

## 5. 审计记录：原子单位与可测量性

决策 5（两周后按命中率裁剪）要求记录从第一天就可统计。原子单位是**一次任务尝试**——
session 太粗（含多次重试），tool call 太细（裁决挂不上去）。

```
task_attempt:
  task_id, attempt_no
  spec_ref              # 对应哪几条验收标准
  oracle_class          # A/B/C/D
  class_reason          # 哪条静态规则判定的（可回溯误判）
  harness, version      # 含版本，用于关联质量变化
  diff_hash, commit
  supervisors[]:
    role                # 回归 / 规格 / 架构 / 风险
    verdict             # pass / fail
    claims[]            # 具体指出的问题
    tokens, cost
  resolution            # merged / reworked / human_override / escalated
  linked_defects[]      # 事后回填
```

两个承重字段：

**`resolution`** 决定告警是否算命中。worker 改了且改完通过 → 大概率真；人站 worker 一边驳回监工
→ 假阳性；人直接覆盖合并 → 假阳性。**此字段事后填写**。缺了它，两周后只有"监工说 fail"的计数，
无法区分噪音。

**`linked_defects`** 捕获假阴性——四道监工都放过、事后才炸的问题。发现 bug 时靠
git blame → commit → task_id 反查当时裁决，才知道本该哪个监工拦住。缺了它只能优化误报，
永远看不见漏报。

`tokens/cost` 同样必需：裁剪判据不能只有命中率。命中率 30% 但便宜的监工，可能优于命中率 40%
但贵 5 倍的。

### 5.1 两周后的裁剪判据

每个监工计算三个数：命中率、漏报数、单位命中成本。

- 命中率低**且**漏报为 0 → 其管辖领域本无问题，降级为抽检
- 命中率低**但**有漏报 → 它没干活，重做提示词而非关掉
- 命中率高但成本高 → 保留，考虑缩小输入范围降本

---

## 6. 架构与开源件映射

```
录音
 └─> FunASR / Paraformer-large-zh-en ──> 带时间戳转录
      └─> LLM 抽取（强制每条需求引用转录片段 + 单列「假设/未决」清单）
           └─【闸门 1：人确认规格】← 产出物是可执行验收条件，不是散文
                └─> OpenSpec / spec-kit ──> spec.md + tasks.md
                     └─> 静态规则分级 ──> 每任务打 A/B/C/D
                          └─【闸门 2：技术方案评审】
                               └─> harness adapter 层【自建】
                                    ├─ openclaw ACP ──> Claude Code / Codex / qwen-code
                                    └─ dagger/container-use ──> 每任务一容器 + git 分支
                                         └─> 四监工并行 review
                                              └─> 工头裁决（≤3 轮，超限升级）
                                                   └─> Hooks + JSONL ──> 审计存储【自建】
                                                        └─> PRD↔diff 一致性检查【自建】
                                                             └─【闸门 3：交付验收 = 看报告】
                                                                  └─【D 类硬闸门：人执行】
```

### 6.1 开源件选型

| 层 | 项目 | License | 用法 |
|---|---|---|---|
| 语音 | `alibaba-damo-academy/FunASR`（Paraformer-large-zh-en） | MIT / CC-BY-4.0（权重） | 直接用。唯一显式为中英混说设计的开源 ASR |
| 语音 | `pyannote/pyannote-audio` | MIT | 说话人分离，语言无关 |
| 规格 | `Fission-AI/OpenSpec` | MIT | 轻量首选，不锁 IDE |
| 规格 | `github/spec-kit` | MIT | 备选，更成熟也更重 |
| 规格 | `bmad-code-org/BMAD-METHOD` | MIT + 商标限制 | 只借鉴其 PRD 生成阶段的角色设计 |
| 调度 | `openclaw/openclaw`（ACP `@openclaw/acpx`） | MIT | **核心底座**，见 6.2 |
| 调度 | `BloopAI/vibe-kanban` | Apache-2.0 | 最强现成先例，研究/借鉴其队列与 worktree 模型 |
| 隔离 | `dagger/container-use` | Apache-2.0 | 每 agent 一容器 + git 分支，MCP 接口 |
| 可观测 | Claude Code 原生 OTel | — | 只有元数据，**对话内容不导出**，仅用于成本/健康度 |
| 可观测 | Claude Code Hooks + `~/.claude/projects/*.jsonl` | — | **真正的审计钩子** |
| 可观测 | `dylibso/claude-code-otel` | MIT | 一条命令起 Docker+Grafana+Prometheus |
| 可观测 | `langfuse/langfuse` 或 `Arize-ai/phoenix` | MIT / Apache-2.0 | 通用 OTLP sink |
| 溯源 | CycloneDX 1.7 **CDXA** | — | 声明+证据/反证格式，贴合「证明此 diff 由 agent X 产出」 |
| 溯源 | `in-toto/attestation` | Apache-2.0 | 声明信封 |
| 门禁 | Semgrep / ScanCode / ORT / CodeQL | 多为 OSS | 静态扫描、许可证合规 |
| 中文 CLI | `QwenLM/qwen-code`、`MoonshotAI/kimi-cli`、`bytedance/trae-agent` | Apache-2.0 / MIT | 可接入 ACP |
| 路由 | `musistudio/claude-code-router` | MIT | Claude Code 指向任意后端 |

### 6.2 openclaw 作为底座的理由与风险

openclaw 表面定位是「个人 AI 助理网关」，容易被误读。关键在于它的 ACP（Agent Client Protocol）
后端 `@openclaw/acpx`：可将 Claude Code、Codex、Cursor、Copilot、Gemini CLI、OpenCode、Droid
作为子进程拉起并控制，每个带独立 auth/model/sandbox，通过 `/acp spawn`、
`sessions_spawn({runtime:"acp"})` 调度。**这正是本设计所需的调度机制，已经存在。**

两个必须知晓的约束：

- **Swarm（多 agent 扇出）标记为 experimental** — 关键路径不压在上面。监工扇出自己实现。
- **SECURITY.md 明确：not designed as a shared multi-tenant boundary**，工具默认在宿主机运行，
  session 归属仅是易用性特性、非安全边界。决策 1（内部工具）下可接受。

`NousResearch/hermes-agent` 是同类竞品而非互补件（同为网关 + 多渠道 + TUI + cron + 子 agent +
多后端沙箱，且提供 `hermes claw migrate` 从 openclaw 迁移）。其「多模型」指换底层 LLM，
**不编排其他 CLI**。作为参考对比保留，不作主选。

两者均不提供 PRD 生成与审计能力。

---

## 7. 必须自建的三块

调研确认这三处无任何开源方案：

### 7.1 harness adapter 层

LiteLLM / Bifrost / Portkey / Envoy AI Gateway / Kong 全部工作在 OpenAI/Anthropic **API 层**，
不认识「Claude Code 的一次 CLI 会话」与「Codex CLI 的一次会话」的差异（不同进程、flag、
权限提示、session 日志格式、沙箱）。`mozilla-ai/any-agent` 最接近，但包的是 SDK 层框架
（LangChain / LlamaIndex / smolagents），非终端 CLI。

契约刻意做小——契约越小，接入新 harness 越便宜，而这正是「换得快」的价值所在：

```
run(task, workspace, limits) -> AttemptResult

AttemptResult {
  diff, transcript_path, tool_calls[],
  exit_status, tokens, cost, wall_clock
}
```

每个 adapter 负责：二进制调用与 flag、权限模式配置、transcript 定位与格式解析、diff 提取。

### 7.2 审计存储

见第 5 节。Claude Code 的 JSONL 格式是**无文档、仅隐式版本化**的格式，
`npm i -g @anthropic-ai/claude-code@latest` 可能静默改变字段。解析器需按此假设编写，
并对格式漂移有检测与告警。

### 7.3 PRD ↔ diff 一致性检查

MetaGPT / ChatDev / spec-kit / OpenSpec / BMAD 均不提供。spec-driven 社区自己承认两个失败模式：
**spec rot**（规格过期后 agent 会跟着过期规格而非当前代码走）与 **agent 中途静默偏航**。
规格存在不等于规格被遵守。

签名式 AI 代码溯源亦无标准——`Co-authored-by:` 类 git trailer 只是约定，默认不签名。

**这是本项目真正的差异化。调度是捡现成的，验收才是护城河。**

---

## 8. 会复利的资产：runbook 规则库

现状：每个项目手写一份 runbook 放在 wiki，用 `[[驭流-打包部署流程]]` 互相链接。问题有二：

1. **知识不复利** — 驭流踩的坑靠手工「参考范式」搬到 smy，第三个项目还得再搬。
   `docker restart` 不换镜像这类陷阱应当一次学会、全局生效。
2. **无强制执行** — runbook 写了验证步骤，但无机制保证 agent 真的跑了、真的看了结果。

设计：把 runbook 从静态 markdown 变为**可执行的分层规则库**

- **全局规则** — 通用陷阱：`docker restart` 不换镜像、`--load` 必须加、端口冲突预检
- **项目规则** — 特有项：端口 8012、`TORCHAI_API_KEY` 等必需环境变量、
  产物中不得出现 `localhost:8000`
- **增量** — 新踩的坑追加成规则，之后所有项目自动继承

回归监工直接消费该规则库作为检查表。第三个项目的部署不再是「参考范式手工搬」，
而是继承已有规则 + 声明差异。

---

## 9. 里程碑

决策 4 选择完整商业项目。策略是项目照做，**自动化覆盖面逐步扩大**，而非第一天就全盘信任。

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| **P0** 骨架 | adapter（先接 1 个 harness）+ 回归监工 + 审计存储 + 静态分级规则 | 一个 A 类任务端到端跑通，审计记录字段完整 |
| **P1** 真实项目 A 类 | 目标项目的 A 类任务全部入流水线，四监工全开 | 闸门 3 上人平均打回次数 ≤ 1 |
| **P2** 测量与裁剪 | 按 5.1 判据算三个数，裁剪监工配置 | 每监工有命中率/漏报/单位成本数据 |
| **P3** 扩到 B 类 | B 类进入自动执行 + 对抗 review | B 类漏报未上升 |

C / D 类始终保持人工，不进入自动化范围。

**P1 的关键指标**：闸门 3 上人平均要打回几次才能验收通过。若每次都需人下去读代码找问题，
说明验收条件不够机器可判定——**问题在闸门 1，不在 agent**。该指标同时检验验收系统本身，
而非仅检验 agent 能力。

---

## 10. 风险与对策

按严重度排序：

| # | 风险 | 证据 | 对策 |
|---|---|---|---|
| 1 | **分级漏判**：C 类任务被标为 A 而无人执行 | 本设计 3.4 已知代价 | 静态规则优先 + 审计可回溯 `class_reason` + 漏判事后转化为新规则 |
| 2 | **中途换 harness 导致架构不一致** | Cognition：上下文丢失与错误传播 | 同一项目内 harness 锁定，跨项目才切换；路由表变更记入审计以关联质量变化 |
| 3 | **context rot 导致长任务质量衰减** | 性能随上下文增长下降，与 token 上限无关 | 任务粒度切小；监工用干净上下文而非续用 worker 会话 |
| 4 | **代码慢性退化**（测试全绿但仓库变差） | GitClear：churn / 重复度上升、有意义重构减少 | 架构监工专职拦截；P2 裁剪时优先保留它 |
| 5 | **监工盖章通过**（review 沦为形式） | 自评估弱点 | 三产证据一出意见的结构；回归监工自己跑命令而非读 worker 总结 |
| 6 | **JSONL 格式漂移致审计断裂** | 无文档、仅隐式版本化 | 解析器加格式检测与告警；升级 harness 视为需验证的变更 |
| 7 | **破坏性操作事故** | Replit 删库（2025-04） | D 类硬闸门非旁路；agent 仅生成脚本，人执行 |
| 8 | **规格过期（spec rot）** | spec-driven 社区共识 | PRD↔diff 检查以当前代码为准；规格变更须走闸门 1 |

---

## 11. 调研可信度说明

本设计的开源件选型基于 9 份并行调研报告（1 份因搜索配额耗尽未完成）。

- **项目存在性与功能描述**：可信，多数经 GitHub API 或官方文档确认
- **星标数据**：**不可信**。多个调研 agent 撞上 GitHub API 限流（未认证 60 req/hr），
  部分数字明显失真（openclaw 385k★、hermes-agent 226k★、spec-kit 125k★ 均不合常理）。
  选型依据结构与功能，不依据星数
- **未完成的对抗性核查**：原计划的逐条联网复验因配额耗尽未执行

需在实施前自行复验的项：`stravu/crystal` 是否已改名 Nimbalyst 且仍开源；
`eyaltoledano/claude-task-master` 的 Commons Clause 条款（不得转售/托管为服务）；
Codex CLI 当前是否已支持 OTel 或 hook（调研时仅见 issue 请求，未确认合并）。
