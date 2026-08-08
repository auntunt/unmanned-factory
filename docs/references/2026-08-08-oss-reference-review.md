# 开源参考批判式审查：prime-agent 与 verifiers

日期：2026-08-08
状态：审查完成，未实施任何代码改动
范围：`PrimeIntellect-ai/prime-agent`、`PrimeIntellect-ai/verifiers`

**一句话结论**：两个项目都不在我们这一层。prime-agent 占的是 `HarnessAdapter` 的槽位，
verifiers 占的是「一个 rollout 怎么打分」的槽位。我们的 A/B/C/D 可验证性分级在这两个项目里
**没有对应物**，也没有在别处找到对应物。可抄的是若干具体机制，不是架构。

---

## 0. 怎么读这份文档

引用分三档，混在一起会让「看过」和「没看过」长得一样：

| 标记 | 含义 |
|---|---|
| **[已核]** | 我本人读过该文件那几行，或用 curl 拉到原文核对过 |
| **[文档]** | 来自项目自己的 README / docs，未对着源码核对 |
| **[子代理]** | 来自子代理的报告，我未逐行核对 —— 当线索看，不当结论 |

第 5 节单列所有 **[子代理]** 未核项。

---

## 1. prime-agent

### 1.1 是什么，坐在哪一层

一个 RLM（Recursive Language Model）架构的 coding agent：模型只有一个内置工具 `ipython`，
读写文件、跑命令、调 skill、派子 agent 全从那个常驻 IPython kernel 出发；kernel 状态跨
tool call 和 compaction 存活。会话由 daemon 常驻 worker 持有，关掉终端只是 detach。
子 agent 是 `rlm(...)` 调用，返回的是准入 handle，不是答案；答案只从 `agent_message` 或文件回来。**[文档]**

对我们来说，它整个是 `factory/harness/base.py` 里那个只有一个 `run()` 的 Protocol 后面的东西。
不是竞品，是候选被插件。

### 1.2 值得抄的

**(1) workspace 未变则不重跑 gate。** 它用 `GitWorktreeSnapshot`（status + diff + untrackedHash）
比对两轮之间的工作区，未变就短路，返回
`not rerun: workspace unchanged since previous failed gate`。
文件 `packages/coding-agent/src/core/autonomous.ts`。**[已核]**

我们的 `factory/dispatcher.py` `_loop` 里没有这个短路。`result.diff_hash` 已经算好并落库了，
比对两轮几乎免费。它只在一种情况下咬人：一个 diff 过了全部确定性闸门、被 spec 或架构监工判红、
然后原样重提 —— 这时每轮要多付两次模型监工的钱。附带收益比省钱更重要：
现在「worker 根本没动」和「动了但还是错」在报表上长得一样。

**(2) typed hooks + 带版本号的会话格式。** 26 个有类型的扩展 hook；session JSONL 第一行是
`{"type":"session","version":3,...}`。**[已核，见 `/tmp/pa/json.md`]**

可泛化的那一条是：**任何 adapter 都该声明自己的格式版本和承重字段**。
我们的 `factory/harness/drift.py` 已经在对 Claude Code 做这件事（`_LOAD_BEARING` + `_missing_fields`），
理由也写在 `claude_code.py` 里 —— `claude -p` 的 JSON 没有版本号也没有文档，字段一改名就会
静默降级成「成功且免费」。prime-agent 证明了这个约定可以由 harness 那边先给出，而不是每次都由
我们反向猜。

**(3) 撤回的一条。** 我原先建议抄它 `autonomousTokenDelta` 里排除 `cacheRead` 的做法。
读完 `factory/metrics.py` 后撤回：那里 `tokens` 只累加、从不进任何判定，进判定的只有 `cost_usd`。
这条在我们这里不咬人。

### 1.3 明确不要抄的

**它的 pathspec 排除清单。** 同一个 `autonomous.ts` 里，快照比对带着
`:(exclude)verification`、`:(exclude)target`、`:(exclude).vf-prime-agent`、`:(exclude)Cargo.lock` 等。**[已核]**

这正是一张「让改动可以躲开闸门的清单」。我们仓库里刚补的 `shadow_code()`
（`factory/harness/workspace.py`）解决的是同一类洞的另一个面：被 `.gitignore` 挡住的新代码文件
四道闸门都看不见，但 check 会执行它。抄这张清单等于自己动手挖回来。

**它的 autonomous gate 整体是反面教材：**

- gate 命令在同一个工作区、同一套权限下跑 —— worker 能改自己的卷子。我们靠
  `runner_hooks()` + `shadow_code()` + 金丝雀探针三面挡这个。
- `gates.commands` 为空时直接 `return undefined` 然后继续跑到限额 —— 不是 fail-closed。
  我们的回归监工是 fail-closed 的，这条不能松。**[已核，`packages/coding-agent/src/core/autonomous.ts`]**
- 没有范围监工，没有规格监工，没有派发前分级。
- 它的 README 自己承认：*"A passed gate checks only what that gate verifies"*。**[文档]**
  这是一句诚实的免责声明 —— 而我们在同一个位置有机制。

### 1.4 不建议把它接成 harness

两个理由，都是硬冲突：

1. **daemon 常驻 worker 按设计就会造孤儿。** 我们的超时是杀进程组（TERM → 3s → KILL），
   起因是实测有 12 个孤儿在停机后继续烧钱。一个「关掉客户端 worker 继续活」的 harness
   跟这条止损线直接对撞。
2. **`rlm(...)` 递归子 agent 让成本变成一棵树。** 它用 `child_usage_attributed` 记账，
   朴素解析会少算。我们的漏账熔断器（连续 2 次记不上账就停机）对这个失效模式免疫 ——
   但那是止损，不是准确记账，会变成一停一停。**[文档]**

想要模型多样性，改监工模型就够了（`--model` 已经接通），不必换 harness。

---

## 2. verifiers

### 2.1 是什么

RL 训练用的环境与评分库。核心抽象：`TaskData` / `Task` / `Harness` / `Runtime` / `Trace` /
`Judge` / `Env`；打分函数分 `@vf.reward`（进 reward）和 `@vf.metric`（只记录）；
judge 通过 `JudgeView` 拿到它该看的东西。关键词是 RLVR 和 reward hacking。**[文档]**

### 2.2 反预期结论：它没有可验证性分级

我事前预测这里能找到喂给 A/B/C/D 的现成分级。**预测错了一半。**

它按「用哪个 judge」分类，不按「这活能不能无人化」分类。而且它**没有「升级给人」这一档** ——
RL 要求每个 rollout 都产出一个数，没有「这题没有廉价裁判，交给人」的出口。

所以：**A/B/C/D 分级在开源生态里没有对应物。** 这不是「还没找到」，是结构性的：
训练框架的目标函数不允许存在一个不产出数字的分支。这一条要记在项目笔记里 —— 它决定了
这一层的自建是必要的，不是重复劳动。

### 2.3 值得抄的：伪造绿清单

最有价值的单条是 **PR #2152 的结论：把测试藏起来没用**。因为那类攻击既不碰测试也不碰评分脚本，
只有**在另一个盒子里判**才有效。**[子代理]**

这条跟我们仓库自己的教训对得上（「验效果，不猜机制」—— 金丝雀穿透三种伪造绿，误拒率比猜机制还低）。
它是外部独立到达同一结论的一个数据点。

其余三条：

- **judge 失败要 raise，不能打 0 分。** 打 0 分会让「判不了」在报表上长得像「判过了，是坏的」。
  `verifiers/v1/judge.py:101-106`。**[子代理]**
- **`Trace.rewards` 用 `None` 表示「没跑」**，不用 0.0。跟我们
  `factory/metrics.py` 里 `hit_rate` 在 `adjudicated == 0` 时返回 `None` 是同一个决定。**[子代理]**
- **judge 的输入要放在被判方碰不到的地方**：SHA 留在宿主内存里，临时路径加 nonce。
  `verifiers/v1/utils/git.py:6-7, 31-35`。**[子代理]**

### 2.4 两处它比我们差

**(1) 它的 restore 步骤是 fail-open。** `logger.warning` 之后照判。
`shared/test_patch.py:197-205, 215-222`。**[子代理]**
对照我们的规矩：静默降级永远更好看，所以必须 fail-closed。

**(2) 它的界定符是反的。** 不可信的 `{response}` 用固定围栏
（`judges/rubric.txt:13-16`），参考答案用动态围栏（`rubric.py:212-216`）。**[子代理]**
正好搞反了 —— 固定围栏应该给可信的那一侧，不可预测的界定符要给不可信的那一侧。
我们的做法（用随机哨兵隔离 worker 文本，不净化也不检测注入）在这一点上是对的。

### 2.5 一个野生标本

`docs/v1/architecture.md:23` 承诺了一处拦截，代码里不存在 —— 对应的 PR #1914 关闭未合并，
grep 命中 0 次。**[子代理]**

这是「测接线，不只测行为」在别人仓库里的野生标本：文档、PR、架构图三样都在，
只有那根线没接上。值得留着，因为它说明这类洞不是我们独有的手艺问题。

---

## 3. 对照表

| 能力 | prime-agent | verifiers | 本项目 |
|---|---|---|---|
| harness / agent 执行 | ✅ 完整 | — | 接别人的（`HarnessAdapter`） |
| 单次结果打分 | 简陋（一条 gate 命令） | ✅ 完整 | 回归监工（确定性、fail-closed） |
| 可验证性分级（能否无人化） | ❌ | ❌ | ✅ A/B/C/D，**无外部对应物** |
| 「没有廉价裁判 → 叫人」出口 | ❌ | ❌ 结构上不可能 | ✅ C 类升级 |
| 不可逆操作硬闸 | ❌ | — | ✅ D 类 blocked_hard_gate |
| 范围监工 | ❌ | — | ✅ |
| 规格监工（对着验收原文看 diff） | ❌ | 部分（judge） | ✅ |
| 伪造绿防御 | ❌ gate 与 worker 同权限 | 部分，且 restore fail-open | ✅ 四道闸门 + 金丝雀 |
| 成本止损 | 有限额，成本是树 | — | ✅ 预算 + 运行时长 + 漏账熔断 |
| 记账/审计 | JSONL + 版本号 | Trace | ✅ audit.db（明文密钥由 `redact.py` 拦） |

---

## 4. 由此产生的三个待办

审查过程中在本仓库找到三处可修点。**本会话按约定未实施任何代码改动**，列在这里等排期。

1. **`_loop` 缺 diff 未变短路**（来自 prime-agent 借鉴）。`diff_hash` 已有，比对近乎免费。
   收益：省两次模型监工调用；把「没动」和「动了还是错」分开。

2. **10 道确定性前置闸门全报 `SupervisorRole.REGRESSION`**，而 `supervisor_metrics` 只按 role 分桶
   （`factory/metrics.py`：`b = bucket(str(v.role))`，claims 里的 `check` 名从不聚合）。
   后果：regression 的 hit_rate / cost_per_hit 是 11 件事的混合数，`verdict_line()` 对它无意义，
   一道从不触发的闸门在报表上不可见，单闸门误拒率拿不到。
   仓库里已有先例可照抄：入口闸门的 `RULE_CODES`（6 个码）+ `rule_code()` + Journal 的
   `gate` / `gate_overruled` 汇总，`test_gate_metrics.py` 覆盖的是那一套、不是这一套。
   最小修法：给这 10 个 check 名建稳定码表，在 role 下面加第二层分桶。
   涉及的 10 个名字：`harness`、`diff`、`git-hook-touched`、`shadow-code`、`runner-hook-added`、
   `fake-green`、`git-config-touched`、`index-skip-flag`、`diff-suppressed`、`spec-criteria-mutated`。

3. **`ProbeResult.skipped` 只有测试在读，生产从不读。**
   `grep -rn '\.skipped' factory tests` 只命中 `tests/test_fake_green.py:122,190,205,216`，
   `factory/` 下 0 命中；`dispatcher.py` 的探针调用点只读 `res.fake_green`。
   于是 `probe()` 的四个 skip 出口全部静默放行，claims / audit.db / metrics 里什么都不留 ——
   跟 `ProbeResult` 自己的 docstring（「`skipped` 和 `fake_green=False` 必须分开」）直接矛盾。
   其中「金丝雀文件名已存在」这个出口可被主动利用：`CANARY_NAME = "test__factory_canary__.py"`
   是硬编码可读常量，worker 自己建一个同名文件既不在 `_RUNNER_HOOKS` 里（躲过 `runner_hooks`）
   也不是 ignored 文件（躲过 `shadow_code`），看起来只是「worker 加了个测试」，
   实际永久关掉了金丝雀。
   最小修法：`skipped` 为真时记一条不改判决的 claim（只留证据）。
   之后可以再考虑随机化 `CANARY_NAME` —— 但先要可见性，因为现在连「这条有没有触发过」都不知道。

---

## 5. 未核实的部分

以下是 **[子代理]** 档、我没有逐行核对的：

- 第 2.3 / 2.4 / 2.5 节的所有 verifiers 行号引用。
- 子代理关于 sandbox 的那组结论（其 Q5），以及测试覆盖结论的一部分，
  来自它自己派的并行子研究，未落到具体文件行。**不作为结论使用。**

要把这些升级成 **[已核]**，路径是 clone verifiers 后对着这几个文件行读一遍。
在此之前，第 2.3 节那四条当「值得去验的线索」，第 2.4 节那两条当「疑似」。

另需记录一次本次会话的操作教训（已写入长期记忆）：承重值不要和一堆噪声共用一个输出通道 ——
`base-create` 的 `base_token` 被 `tail -40` 冲掉过一次，四条找回路径全死。
从此这类输出走 `--jq` 只取承重字段，schema 另外单独读。
