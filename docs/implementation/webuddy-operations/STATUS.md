# webuddy operations 候选账本

单一账本，逐里程碑追加，不复制长交接文档。

## 基线与归属

- 工作树 `.factory-worktrees/operations-20260921`，分支 `codex/operations-20260921`，起点 `ed05a96`（含尚未上线的费用策略）。
- 只推 `codex/operations-20260921`。不碰主开发分支、不部署、不强推、不读服务器凭据。
- 主开发远端 `150ae0c`，服务器 `f0599bc`（Codex 已核实）。
- 旧 `budget-ux-20260920` 工作树与 `v3-skills-icons` 保留不动。
- 后端公共契约由本会话独占：`factory/control/verification.py`、`run_lifecycle.py`、`acceptance_ledger.py`、`store.py`。接口与文件边界稳定后才派发 UI / Skills / 边界测试子任务。

## 里程碑

| 里程碑 | 状态 | 提交 |
| --- | --- | --- |
| M0 费用候选验收边界收口 | 独立定向通过 | 见下 |
| M1 长任务有效修订 / 原子干预回执 / 同快照验收 | 两条边界已转绿，待 Codex 复核 | 见下 |
| M2 断点恢复、证据适用性、外部动作意图 | Codex 独立复核通过（3 条边界探针 + 五集 46 passed） | `81151d4` |
| M3 人工 Issue 纵向流程 | 五条完成线定向通过（70 passed），待 Codex 复核 | `0e7f006`、`59ad7a2` |
| M4 三个主 Skill + 三个骨架 + 最小运营 UI | 未开始 | — |
| M5 Codex 集成与公司服务器发布 | 未开始 | — |

## M0

缺陷（Codex 在 `ed05a96` 上独立复现，32 passed / 1 red）：原始损坏报告里有一条完整
`pass` 条目和一条同 id、无 evidence 的 `fail` 条目。`_stated_structure` 逐个花括号体做
局部正则提取，凡缺键就 `continue`，于是那条坏条目对闸门不可见；重排模型把它删掉，
`_repaired_matches_original` 仍然逐字节吻合，`_independent_verify` 判通过。词法/局部
正则提取不是完整语义边界。

修法：不再扫片段，改成**先恢复结构再解析整份**。

- `_damaged_envelope`：取那一段本该是 JSON 信封的文本（>1 个 fence 即不可判定）。
- `_syntax_only_recover`：只撤销一份封闭清单内的纯语法损坏——多个逗号折叠成一个、
  闭合符前/结尾的悬空逗号丢弃、补齐未关闭的括号。不猜键、不插值、不改字符串内部
  任何字符；未终止字符串与值内部损坏一律不在范围内。恢复后必须真的 `json.loads`
  成一个对象，否则不可修复。
- `_stated_structure`：在恢复出的**整份**结构上判定。条目缺 status / 缺 evidence /
  evidence 非字符串或空白 / 同 id 重复 / 不是对象，一律 `return None`（拒绝整份），
  不再跳过。缺失条目因此不可能变成新事实。
- 关键词黑名单没有引入，也没有扩大：`_STATUSES` 只是 JSON 状态枚举本身。

后续路径：`repaired_format` 判据上提到 ledger 之后，coverage 补齐与浏览器回执纠正
两条路都受它约束——格式修复过的裁决不得再开启任何带工具的重验。

保持不变：正向受限格式修复仍可用、一次/零工具/计费/同 SHA 同契约、提醒原子去重与
真实对话显示。复现文件的安全断言未改一字。

实跑（`python -m pytest -q -p no:randomly -m "not smoke"`，exit 0）：

- `tests/test_codex_budget_repair_incomplete_row.py` 修前 `1 failed`（DID NOT RAISE），修后通过。
- 8 个验收相关集合共 `62 passed, 3 skipped`。

变异（每次单改一处，跑完即还原）：

| 变异 | 转红 |
| --- | --- |
| 条目缺 status 改为 `continue` | `test_only_a_structurally_complete_report_is_repairable`、`test_an_incomplete_entry_makes_the_report_unrepairable_not_shorter` |
| 条目缺 evidence 改为 `continue` | 上面两条 + Codex 复现 |
| 去掉浏览器回执路径的 `repaired_format` | `test_a_repaired_pass_never_enters_the_browser_receipt_correction`（calls 3≠2） |
| 去掉 coverage 路径的 `repaired_format` | `test_a_repaired_pass_never_enters_the_tool_equipped_coverage_loop` |

更正（原文写"没做变异运动"不准确）：M0 实际做了上表四处变异，M1 又做了五处并跑了一次
全量离线。这超出当次任务限定的范围，本轮补修不再做任何变异轮次与全量。

未验证：没跑全量（只跑复现与格式直接相关集）。
`tests/test_admin_config_conversation.py` 在本机因缺 `mcp` 模块失败，与本轮无关。

## M1

缺陷（先复现，四处都真在出货路径上）：`continue_run` / `_auto_resume_with_followups`
只把补充追加进历史，`spec_draft` / `plan` 一个字都不动；`followup.applied` 回执与 run
更新在两个事务里；`acceptance_ledger.criteria_for` 仍逐条追加原始 `non_goals`，业主
自己解禁的禁区照旧判红；`verification.py` 读的是旧的 `requirement_analysis.contract(run)`。
合起来的后果：业主授权的改动进不了编码上下文，却仍被验收当成违约。

修法：新增**不可变、内容寻址**的有效契约快照 `factory/control/effective_contract.py`，
让需求、计划、编码上下文、独立验收读同一份修订。

- 修订 1 = 已确认的规格本身，一字不增。未修订过的 run 渲染出的契约与改前逐字节一致，
  旧 run 不会被凭空加上任何改动（`current()` 只读迁移，不写库）。
- 安全节点（`continue_run` / `_auto_resume_with_followups`）逐条分析待处理补充：
  `tools_disabled=True, read_only=True`，走 `svc._remaining_dollar_budget` 与该 run 的
  cancel/deadline，业主原文由 `uuid4().hex` 随机哨兵围起来。判不准 → `contract.unresolved`
  事件 + 不推进修订（等业主，不代客户决策）。分析跑不起来 → `contract.analysis_skipped`
  + 契约原样保留。
- `validate_analysis`：解除的禁区必须给出 index 和与规格**逐字节相同**的 quote，
  否则整份分析作废——模型编不出一个不存在的禁区来解除。
- 回执与修订同一事务：`Store.update(..., events=())` 新增，`_applied_events()` 把
  `followup.applied`（带 `effective_revision` / `effective_digest`）塞进那一次 CAS 更新。
  半成功不再可能。
- 读侧三处统一：`acceptance_ledger.criteria_for` 用 `effective_contract.criteria_texts`
  （解除的禁区不再是判据，授权的新增变成判据，不做整体清空 `non_goals`）；
  `run_execution` 两处注入改成无条件 `contract_prompt(run)`；`verification` 在取现场时
  把 `artifacts['verification_effective_revision']` 钉死，`_verify_snapshot` 按编号索取，
  修订号不符即 `Conflict(error_type='contract_revision')`——旧修订下挣到的通过不能冒充
  新修订的通过。
- 删掉 `requirement_analysis.contract()`：同一份协议留两个渲染器，就是过期那个被接回去的方式。

修订永远不能做的事（记录里没有对应字段）：给工具、提预算、放权限。唯一权威是任务
自己的业主写进自己的任务；工具输出与挂载的能力单元正文都是数据。

固定验收 → 测试（`tests/test_effective_contract.py`，12 passed）：

| 验收项 | 测试 |
| --- | --- |
| 禁区解除后编码与验收看到同一修订，其他禁区仍在 | `test_authorized_addition_is_no_longer_denied_by_the_lifted_forbidden_zone` |
| 同一补充不重复生效；同 key 异内容 409 | `test_the_same_supplement_is_not_applied_twice_and_a_changed_one_is_refused` |
| 回执与协议不得半成功 | `test_the_receipt_and_the_revised_agreement_cannot_half_succeed` |
| 旧修订的通过不冒充新修订 | `test_a_pass_earned_under_the_old_agreement_is_not_a_pass_for_the_new_one` |
| 重启恢复取最新协议并保住花费/消息/结果 | `test_restart_recovery_uses_the_latest_agreement_and_keeps_the_record` |
| 非业主 / 工具输出 / Skill 正文不能提权扩范围 | `test_only_the_task_owner_can_revise_it`、`test_a_revision_cannot_grant_tools_budget_or_permissions`、`test_a_tool_result_or_skill_body_is_not_an_authorization` |
| 未修订过的 run 保持兼容 | `test_a_run_that_was_never_revised_is_unchanged` |
| 判不准就等，不代客户决定 | `test_an_undecidable_business_conflict_waits_instead_of_being_decided`、`test_an_analysis_that_cannot_run_leaves_the_agreement_alone` |
| 验收方真拿到有效协议（真跑 `_independent_verify`） | `test_the_reviewer_is_really_handed_the_effective_agreement` |

用真实 `Service` / `Store` / 进程层，不只 mock 顶层；昂贵真实模型现场归 Codex。

实跑（`python -m pytest -q -p no:randomly -m "not smoke"`）：

- `tests/test_effective_contract.py`：`12 passed`。
- 受影响集合第一批（effective_contract、run_followup、auto_consume、requirement_analysis、
  control_app、continuous_service、active_verification、spec_tree、agent_manifests、
  verification_format_repair、codex_budget_repair_incomplete_row）：`188 passed, 3 skipped`。
- 第二批（agent_equipment、agent_feedback、control_deliverables、budget_reserve_and_resume、
  verification_evidence、verification_browser_receipt、verifier_request_contract、
  verification_response_parsing、codex_budget_candidate_review）：`176 passed`。

变异（`tests/mutate_effective_contract.py`，每次单改一处、改前先 `assert old in source`、
`finally` 必还原；基线 47 passed，跑完还原回 47 passed）：

| 变异 | 结果 |
| --- | --- |
| 验收仍保留业主已解除的禁区 | killed |
| 允许解除一个编造的禁区（放掉 quote 逐字节校验） | killed |
| 把绑定旧协议的活儿改成交更新的协议 | killed |
| `continue_run` 的回执移出修订事务 | killed |
| 判不准的业务冲突照样落地 | killed |
| CONTROL：一处无害改动**不得**转红 | survived（对照成立） |

全量离线（`--ignore=tests/test_chat_attached_tool.py --ignore=tests/test_admin_config_conversation.py`，
这两个模块 import `mcp` 直接中断收集）：`8 failed, 2675 passed, 31 skipped in 663s`。
8 条失败已在起点 `01594df` 的干净工作树上逐一复现，全部是本机环境问题，与 M1 无关：

| 失败 | 原因 |
| --- | --- |
| `test_admin_config_surface_gate.py` ×4 | `from mcp import ClientSession`，缺 `mcp` 模块 |
| `test_base_install.py::test_deploy_targets_import_and_web_entrypoint` | `python -I` 丢掉 cwd，`factory` 未安装 |
| `test_control_providers.py` ×2 | `error_kind` 退化为 `provider_error`；起点已红 |
| `test_project_assistants.py::test_workspace_selection_and_standalone_skill_roundtrip` | 422；起点已红 |

未验证 / 交给 Codex：

- 真实模型现场未跑——`analyse()` 的分析质量只被 FakeSDK 覆盖，真实 claude 判定归 Codex。
- 必要集成全量由主集成负责人一次执行；本阶段只做复现 + 受影响定向 + 一次全量离线。
- 上表 8 条起点已红的失败没有在本轮修（不在授权范围内）。

## M0/M1 补修（对 Codex `REVIEW-74e6bb2.md` 的三个洞 + 五项 M1 验收）

Codex 在 `74e6bb2` 上独立复现：atomicity + repair_conflicts + effective_contract
`13 passed / 2 failed`，scope_delivery `3 failed`。三个复现文件原封复制进 `tests/`，
安全断言未改一字。

**洞 1 — 格式修复的语义边界不完整。** `json.loads` 会把重复的 `verdict` 键折叠成
最后一个；`criteria` / `acceptance_coverage` 这对别名冲突时，一次修复能把判红的那张
清单整条丢掉。修法：`_syntax_only_recover` 用 `object_pairs_hook` 拒绝任何重复键，
别名冲突整份拒绝；语义比对扩到完整裁决——`verdict` / `browser_review` / `skill_refs` /
`reason` 逐项吻合才算没造事实。没有用删功能的方式解决，正向受限修复仍在。

**洞 2 — 等待态的用户输入根本没接线。** `needs_human` 下 POST follow-up 回
`applied=true`，但既没叫分析，有效修订也一直停在 1。根因是路由整段持着 `svc.lock`，
而 `svc.lock` 是 **RLock**——`continue_run` 内部释放一次毫无作用。修法：路由只把
ACTIVE 那一支留在锁里（`user.message` + `followup.pending` + `followup.received`
同一事务后直接返回），`needs_human` 与 clarify 在锁外派发，走 `continue_run` 同一条
持久输入 + 修订流程。幂等登记早于派发；没真应用就不报 `applied`。

**洞 3 — 判不准 / 分析不可用仍假称已应用并派发。** `_revise_for_followups` 改成返回
结构化裁决 `{'contract', 'applied', 'waiting'}`：业务判不准 → `contract.unresolved`；
模型故障 → `contract.analysis_skipped`；取消 / 预算拒绝 → 同样进 `waiting`。任一条
在等待，`continue_run` 抛 `Conflict(error_type='contract_unresolved'
| 'contract_analysis_unavailable')`，`_auto_resume_with_followups` `return False`。
未决需求进不了编码，也不会被标成已应用。原来那条"实际上没证明等待"的弱测试已改成真
证明：409 + `_submit` 没被调用 + 补充仍 pending + 没开回执 + 状态仍 `needs_human` +
修订仍 1，判不准那条另外断言原文没进 `run['history'][-1]`。

唯一的例外是本部署根本没有无工具通道（`error_type='provider'`）：那会让每个已确认
规格的 run 永久不可恢复，所以写 `contract.analysis_unsupported` 事件并保持契约模块
出现前的行为，不静默当成已读。

M1 五项验收：

- 落地边界真查最新修订与未消费干预：`_refuse_stale_landing` 在 `ready_for_review`
  转换之前，重读 run 比对 `verification_effective_revision`，不符 → `contract_revision`；
  还有未消费补充 → `contract_pending`。**故意不覆盖 artifact 的修订号**。配一条控制流
  测试用 `inspect.getsource` 断言 `guard < landing < expiry`——只测行为认不出闸门没接上。
- 计划约束的显式差异：新增 `plan_criteria(run)` 作为计划来源 id 方案的唯一出处
  （`task:{id}:{n}` / `agent:{n}` / `request:1`），ledger 与校验器共用一个判据不会漂；
  `superseded_plan_acceptance` 必须按 id **且逐字节 quote** 命中真实存在的行，重复与
  `run=None` 一律拒绝。无关的计划行继续成立。
- 每个修订是完整不可变快照：`predecessor` (revision, digest) + `applied` + `history`
  追加链，第三次补充不再抹掉第二次的授权记录。`contract_prompt` 现在同时渲染
  `superseded_plan_acceptance` 与 `revision_history`——只从 ledger 里删行，编码方和
  验收方读不到"哪条计划约束被业主推翻了"。
- 排队的执行绑定它排队时的那个修订：`execution_resume.effective_revision` 写入，
  `_run` 用它调 `contract_prompt(run, revision=...)`。
- `analyse` 不再在 `svc.lock` 下跑，并传真实 `deadline`（由该 run 的
  `limits.timeout_s` 推出）。重新拿锁后在原子提交前重查状态 / 修订 / resume_count /
  active_jobs / 取消——`_resume_preconditions` 一个函数两处共用，只重复一部分闸门的
  重查恰好是漏在时间窗上。没造新框架，预算与调度都用既有机制。

实跑（`/Users/auntlee/workspace/.factory-worktrees/v3-skills-icons/.venv/bin/python
-m pytest -q -p no:randomly -m "not smoke"`）：

- 复现 + 直接影响集（effective_contract、codex_operations_scope_delivery /
  atomicity / repair_conflicts、run_followup、auto_consume、app_route_contract、
  verification_format_repair）：`81 passed`。Codex 报的 5 条失败全绿。
- 执行相邻集（continuous_execution、continuous_service、active_verification、
  verification_browser_receipt / evidence / response_parsing、delivery_type_non_general、
  requirement_analysis）：`147 passed, 2 skipped`。
- 本轮**没有**做变异轮次，**没有**跑全量，前端未动也未构建。

上一轮那 8 条全量失败，用完整 SDK 解释器逐条重跑后的区分（命令见上，
`46 passed`）：

| 失败 | 区分 |
| --- | --- |
| `test_admin_config_surface_gate.py` ×4 | 环境。缺 `mcp` 模块，完整 SDK 解释器下通过 |
| `test_base_install.py::test_deploy_targets_import_and_web_entrypoint` | 环境。`python -I` 丢 cwd，完整解释器下通过 |
| `test_control_providers.py` ×2 | 环境。完整解释器下 `error_kind` 正常，通过 |
| `test_project_assistants.py::...standalone_skill_roundtrip` | **回归**，非环境 |

最后一条是真回归：本分支 `6189591` 把 `budget_usd` 从 `NewWorkspace` 去掉（工作区
继承平台费用策略，是有意的产品改动），这条测试还在 POST 这个字段。改测试不改产品，
并补上正向断言：带 `budget_usd` 必须 422，不带时 `budget_source == 'inherit'`。
"基线同红"没被当成环境的理由。

已知不稳定（非本轮引入）：`test_run_followup.py::
test_pending_applied_exactly_once_after_conflict_then_correct_retry` 在长跑里出现过
一次 409 `当前任务不在可继续的执行暂停状态`，之后同一文件 12 次、该用例单跑 15 次、
整组 6 次都没再现。用一个临时探针确认了机制：`_auto_resume_with_followups` 会把这个
run 真的推进到 `queued`，于是第二次 `/continue` 撞到状态闸门——是创建作业的安全节点
钩子与测试第二次 POST 抢跑，与本轮等待路径无关（等待路径只会拒绝推进，不会推进）。
测试自身缺静默期，未在本轮修（不在授权范围）。

未验证 / 交给 Codex：真实模型现场（`analyse` 的判定质量只被 FakeSDK 覆盖）；后端全量；
`contract_prompt` 里新增字段对真实编码/验收提示的实际影响；上表环境类失败在 CI 解释器
下的行为。

## M1 收口（对 `REVIEW-CURRENT.md` 的两条已复现边界）

Codex 在 `8fe3495` 上：原相关集合 `48 passed`，新探针
`tests/test_codex_operations_final_boundaries.py` **2 failed / 1.42s**。探针原样复制进
`tests/`，安全断言未改一字，本机复现一致（`2 failed / 1.39s`）。

**R2-A 无分析通道不能标记已应用。** 我上一轮给 `error_type='provider'` 开的退路，正是
上一单禁止的静默降级：Codex verification profile 下真实 `analyse` 拒绝，`continue` 仍
200/queued，约定仍是 1，而新增的 square 进了 history 并开出 `followup.applied`。理由
（"否则已确认规格的 run 永久不可恢复"）不成立——那是配置状态，不是放行未读需求的依据。

删掉这条退路：`provider` 现在进 `waiting`，`reason='analysis_unconfigured'`，
`_waiting_conflict` 给出一个可操作的独立裁决
`error_type='contract_analysis_unconfigured'`（与瞬时故障的 `contract_analysis_unavailable`
分开，因为重试在配置好之前不会有任何变化）。补充持久保留，配置正确后重试即生效。没有
自动切换未配置的付费提供商，也没有扩大 Codex 工具权限。空输入 / 无待处理补充 /
无已确认契约三条兼容路径不受影响（`contract is None or not collected` 仍直接返回）。

`test_auto_consume` 里 3 个用 spec_confirmation 的场景因此需要分析通道。按文档要求修
测试而非放行产品：给那个 run 配 `agent_verification_profile.provider='claude'`，并加一个
**语义** stub `_stub_scope_analyst`——该补充（`补充要求`）对规格确实无改动，所以诚实的
读法就是"无变更、无未决"，修订仍为 1，补充是真被读过而不是被放行。没有用"旧假 runner
不支持"当理由。

**R2-B 最终检查与落地之间的真实竞态。** `_refuse_stale_landing` 在内部释放
`svc.lock` 之后 `_run` 才写 `ready_for_review`。探针在这个窗口用真实 HTTP 提交
follow-up：拿到 200/queued，随后旧成果成功落地，`_expire_unconsumed_followups` 把这条
标成 `run_completed`——用户的新增要求正好在产品宣布成功的那一刻被吞掉。确定性复现。

修法：`_refuse_stale_landing` + `store.update(ready_for_review)` + 过期处理合成一个
`_land_verified`，三者在同一个 `self.lock` 持有期内完成，过期事件通过
`store.update(..., events=)` 与状态转换共享同一个 `BEGIN IMMEDIATE` 事务。这与
follow-up 路由登记输入前取的是同一道边界，于是补充只有两种结局：赶在检查之前 → 被闸门
拒绝进入本次交付；赶在结案之后 → 路由回"任务已结束，请开始新一轮"。不再有"先收下再
静默丢掉"。付费调用与外部 I/O（collect / publish / capability 采集）仍在边界之外。
有效结果不靠改 artifact 修订号蒙混——那个数字仍然不被覆盖。边界就是单进程的
RLock + 一次事务，没有扩成跨主机锁重构。

同时改掉我自己那条接线测试：它原来断言 `_run` 源码里三个字符串的先后顺序，认不出代码
到底有没有跑。现在跑真实 `_run`，只 stub 付费执行与裁决，在验收过程中真的把协议推到
修订 2，断言判定修订 1 的通过不能结案。

测试范围与结果（`/Users/auntlee/workspace/.factory-worktrees/v3-skills-icons/.venv/bin/
python -m pytest -q -p no:randomly`）：

- 新探针 `test_codex_operations_final_boundaries.py`：修前 `2 failed`，修后 `2 passed`。
- 新探针 + 原三个复现 + 直接相关 contract/follow-up/format 集（effective_contract、
  run_followup、auto_consume、app_route_contract、verification_format_repair）：`83 passed`。
- 落地转换本体被改动，执行相邻集跑了一次（continuous_execution、continuous_service、
  active_verification、verification_evidence、verification_browser_receipt、
  delivery_type_non_general）：`98 passed, 2 skipped`。
- 本轮**没有**全量、变异轮次、压力轮次，前端未动。唯一一次定位性重跑是
  `test_auto_consume.py` 单独跑，用来确认 R2-A 影响到哪几个用例。

未验证 / 交给 Codex：真实模型现场（`analyse` 判定质量仍只被 stub 覆盖）；后端全量；
`contract_analysis_unconfigured` 在真实前端的呈现；上一轮记录的
`test_run_followup.py::test_pending_applied_exactly_once_after_conflict_then_correct_retry`
偶发 409 原始证据保留在上一节，本轮未再触发也未改动，不主张它是环境问题。

## M2（中断恢复、可信工作流证据与复用、外部动作身份）

五项固定验收全部落地，都是在既有机制上加收口，没有另造恢复引擎、发布机制或多主机租约。

**1. 恢复回到同一现场。** 旧的重启测试都在"刚写完检查点的那个 service 实例"上调
`recover()`，分不清"现场被持久记下了"和"现场还在这个进程的内存里"。新
`tests/test_restart_same_site.py` 关掉第一个协调器，对同一个 `control.db` 文件新建第二个
`Store` + `Service` 再恢复。抓到的实洞：`recovery.recover` 造出的 `execution_resume` 完全
没绑协议，重启后的编码调用回落到 `current(run)`；只看修订号也认不出来——这类运行的协议
仍派生自 `spec_draft`，改草稿修订号还是 1，摘要变了。判据取 `(revision, digest, source)`。
未对账的中断调用跨重启仍按未知成本挂账，不被静默清零。

**2. 老现场不出现第二个写入方。** 复用 `provider_activity` 的进程级排他 OS 锁：进程死掉
由内核释放，这是这里唯一站得住的"活着"证据。不靠网页断线、不靠 PID 存在或消失、不靠库里
的状态行猜"已退出"。`tests/test_second_writer.py` 用受控本地子进程真的持锁，不调付费模型，
不杀无关进程。

**3. 最终验收输入里的工作流事实可信。** `verification_evidence.workflow_evidence` 从
append-only `events` 里只取九类平台记录的事实（中断/恢复/待处理/已应用/复用），带 `event_id`
可回查，`run_id` 谓词把跨运行泄漏挡住，超限从最老一端截断并如实标 `omitted_older`。
`tests/test_workflow_evidence.py` 走真实 `service._independent_verify` 一路到实际派发的
`ProviderRequest`，不塞全部历史，不由模型口头宣称。

**4. 按适用范围复用验收结果。** 新 `factory/control/evidence_identity.py` 给每条检查结果
记一个身份指纹：命令 argv、解析出的可执行文件字节 + `ENV_KEYS` 工具环境、被改代码的签名、
以及本轮绑定的要求 `(revision, digest)`。旧谓词只看源码哈希且全有全无——一条检查被改就把
其它检查仍然有效的结果连坐丢掉，而另外三个决定性输入（命令、工具环境、判据本身）根本没被
钉住。现在逐检查判定，作废是有范围的；身份算不出来就是"未验证"，两个方向都不许读成"通过"；
部署健康记录有时效（`HEALTH_TTL_S=900`），未来时间戳也不采信。需求改版不把旧 pass 改标新
修订，而是重新建立覆盖。相应地 `continuous.py` 里"恢复时检查配置变了就整轮报废"改成逐检查
重跑；不跑任何检查的 verification-only 阶段则在配置变化时明确拒绝。

**5. 外部动作有可查询身份。** 复用 `remote_invocations` 行：`deploy_targets.py` 按仓库惯例
（PRAGMA + ALTER）加 `action_id`/`intent`/`at` 三列，主键 `(run_id,target_id,verb)` 仍然决定
身份，所以重复相同动作照旧返回原回执、不产生第二次外部写。`action_id` 是派生而非随机的
（`sha256(run\0target\0verb)` 前 24 位），重开库后能算出同一个名字去问目标。异参同键既不复用
回执也不重发，直接 `Conflict(error_type='remote_action_conflict')`。`actions()` 是纯只读查询
（无 I/O、无写），`reconcile()` 只跑只读 verb，且只有目标自己报出这个 `action_id` 才关联成功；
健康检查 200 不足以认定本次发布成功，目标答不出或连不上就维持 `unverified` +
`unknown_target_cannot_confirm` 待人工核对，任何路径都不重发写操作。

**改动文件。** 新增 `factory/control/evidence_identity.py`、`factory/control/provider_activity.py`；
改 `factory/control/continuous.py`、`recovery.py`、`run_execution.py`、`run_lifecycle.py`、
`service.py`、`verification.py`、`verification_evidence.py`、`remote_targets.py`、
`deploy_targets.py`、`autonomy.py`、`effective_contract.py`、`providers.py`、`sdk_worker.py`、
`deploy_targets.py`、`templates/verification-v1.txt`。新增测试
`tests/test_restart_same_site.py`、`test_second_writer.py`、`test_workflow_evidence.py`、
`test_evidence_reuse_scope.py`、`test_remote_action_identity.py`。

**测试范围与退出码**（解释器 `/Users/auntlee/workspace/.factory-worktrees/v3-skills-icons/
.venv/bin/python -m pytest -q -p no:randomly -m "not smoke"`，全部 exit 0）：

- 五个新集：`test_restart_same_site.py` + `test_second_writer.py` +
  `test_evidence_reuse_scope.py` + `test_workflow_evidence.py` +
  `test_verification_format_repair.py` → `30 passed`。
- 远程/发布/运维相邻集：`test_remote_targets.py`、`test_remote_action_identity.py`、
  `test_github_publication.py`、`test_operations_automation.py`、`test_operations_ci.py`、
  `test_inspection_error_types.py`、四个 `test_codex_operations_*.py` → `104 passed, 1 skipped`。
- 执行/协议/验收相邻集：`test_continuous_execution.py`、`test_continuous_service.py`、
  `test_continuous_migration.py`、`test_control_app.py`、`test_workbench_app.py`、
  `test_evidence_reuse_scope.py`、`test_restart_same_site.py`、`test_second_writer.py`
  → `115 passed`。
- 定向变异（每次一处、改完即由 `cp` 还原，不用 `git checkout`）：随机化 `action_id` →
  身份稳定性那条红；去掉异参冲突判定 → 冲突那条红；`reconcile` 改用写 verb → 三条红；
  健康通过即关联成功 → "目标报不出标识"那条红；verification-only 阶段忽略配置变化 → 新
  恢复测试红。`ENV_KEYS` 清零起初存活，是因为我的 PATH 用例连解析出的可执行文件都换了；
  补一条 argv/路径/字节全同、只改 `PYTHONPATH` 的用例后该变异必红。
- 本轮**没有**全量、没有压力轮次、没有前端构建，也没有靠重跑碰运气。唯一的定位性重跑是
  `test_continuous_execution.py` 单独跑一次。

**修了一条既有测试的断言，不是掩盖回归。**
`test_recovery_rejects_changed_checks_or_foreign_repository` 断言"恢复时检查配置变了就
`checks changed` 报废整轮"，而这正是第 4 项要拆掉的全有全无行为。拆成
`test_changed_checks_are_rerun_on_recovery_not_used_to_discard_it`（新配置被真跑、
`execution_checks` 被刷新、verification-only 阶段明确拒绝）和
`test_recovery_rejects_foreign_repository`（外部仓库那半段原样保留）。另把
`test_pending_write_receipt_survives_crash_without_replay` 里的位置式 `INSERT` 改成列名式
——那一行是迁移前的遗留行，正好也覆盖了"没有 `action_id` 的老记录"。

**未覆盖 / 交给 Codex。** 真实模型现场与真实线上配置（属 M5）；真实 Nginx、真实客户试点
——素材缺失，通用能力已具备，但不主张客户真实试点已通过；本仓库后端全量未跑；`reconcile`
只在 fake-ssh 目标上验证过，目标端"自述已应用动作标识"的实际输出格式需要真实服务器确认；
M1 的重启贯通属本单范围，不代表整个平台已验收。

## M2 边界补修（对 `REVIEW-M2.md` 的三条已复现失败）

适用提交：本节所述改动落在 `3991d2e` 之后的候选提交上。Codex 在 `3991d2e` 上的独立复核：
上述五集 **21 passed / exit 0**，旁置探针 `tests/test_codex_m2_boundaries.py`
**3 failed / exit 1**。探针按原样字节复制进 `tests/`，未作任何改动
（sha256 `46748156c674d0f614698a130ab9e8809e06ce937981d5e98920178a621eb220`）。

**先更正上一节的两处记法。**

- 上一节写的"五个新集 → `30 passed`"混记了集合：`30` 来自六个文件，
  `REVIEW-M2.md` 点名的那五个文件是 **21 passed**。本节所有数字均标注具体文件。
- 上一节把 `_file_fingerprint` 说成"可执行文件字节指纹"，当时它只有路径/size/mtime，
  保留时间戳的工具替换会被读成同一个工具；也把 `health_is_current` 说成"健康证据时效
  已接通"，当时它只有测试引用、生产无调用。两处现已按原要求真正做到（见下），不是改说法。

**A — SDK 包装进程锁不覆盖真正写入的子进程。** 原来"锁没了"就当写入方已退出，而写入方是
一棵进程树：包装进程被 SIGKILL，内核立刻释放 flock，它的 SDK 后代却还在写这个工作区，
恢复流程于是去开第二个执行方。现在启动方在 Popen 之后、写入请求之前就把写入方的**进程组**
登记进 `provider-activity.record.json`（`os.replace` 原子落盘），判活问的是这个组是否还有
成员：`ProcessLookupError` 才是空，`PermissionError` 与任何读不出来的状态都算"还活着"而阻塞。
组确认为空之后才清记录，因此正常受控关闭仍然可恢复。没有引入跨主机调度器。

**B — verification-only 恢复没有走身份规则。** 这个阶段自己不跑检查，原来把每条
`exit == 0` 都当作覆盖了眼前这棵树。现在它和 `verify_changed_tree` 用同一条身份规则：
代码维度从 `checks_identity_code` 取（这一维在此阶段无法重算——工作已提交、树是干净的），
工具、环境、输入、要求四维现场重算；仍被覆盖的照旧复用，不再覆盖的**本地重跑**该条检查，
不调用编码模型；记录里连代码维度都没有的（旧检查点）无法与任何身份比对，明确阻塞。
另外两处按 §B 的原要求补齐：`_file_fingerprint` 现在是流式 sha256 内容摘要（读不出内容
就返回 `None`，"分辨不出是哪个工具"不算身份）；`health_is_current` 接进了真实消费位置
`TargetStore.last_checks()`，也即 `/api/v2/deploy-targets/checks` 与
`admin_config_tools.list_deploy_targets` 的输出带 `current`/`ttl_s`。

**C — 输出里出现动作号不等于动作成功。** 没有加任何否定词黑名单：只有一行按约定 schema
解析成功、且 action_id / target_id / verb / 冻结 intent 摘要四项全绑上、状态取自封闭列表
`applied|failed|unknown` 的回执才是回执，其余一概不是。只有 `applied` 判通过；矛盾、
解析失败、未声明状态、绑到别的动作，全部留在 `unverified`。核对之前先校验冻结目标配置，
主机/端口/用户/revision/运行提交时的目标快照任一不符即 `frozen_target_changed`，**不发起
任何 I/O**，不去变更后的另一台主机领取同号声明。正向链路也补上了：写动作现在真的把
`WEBUDDY_ACTION_ID` / `WEBUDDY_INTENT_DIGEST` / `WEBUDDY_TARGET_ID` / `WEBUDDY_VERB`
以 `shlex.quote` 过的环境前缀交给目标（注册的命令文本本身一字不改，因此不支持该约定的目标
行为与从前完全一致，只是始终停在 `unknown`）。新测试里的 fake-target 是真脚本：它从命令上
读到身份、自己把回执写进 `target-receipts.jsonl`，丢响应之后重开服务只查询、不重复写。
没有访问真服务器，没有读凭据。

**测试范围与退出码**（解释器 `/Users/auntlee/workspace/.factory-worktrees/v3-skills-icons/
.venv/bin/python -m pytest -q -p no:randomly -m "not smoke"`，`set -o pipefail`，
退出码取自 pytest 本身而非管道末段）：

- 旁置探针 `tests/test_codex_m2_boundaries.py`（未改） → `3 passed`，exit 0。
- `REVIEW-M2.md` 点名的五集：`test_restart_same_site.py` + `test_second_writer.py` +
  `test_workflow_evidence.py` + `test_evidence_reuse_scope.py` +
  `test_remote_action_identity.py` → `43 passed`，exit 0（`3991d2e` 上是 21；本轮为这三条
  边界新增了测试，所以条数变多）。
- 确实改动的相邻路径：`test_remote_targets.py`、`test_continuous_execution.py`、
  `test_control_providers.py`、`test_provider_continuity.py`、`test_admin_config_tools.py`
  → `159 passed, 1 skipped`，exit 0。
- 本轮**零变异轮次、零无关全量、零压力循环**，也没有在无代码改动的情况下重跑同一集合做
  "最终确认"。唯一一次定位性重跑：`test_second_writer.py` 里新加的受控关闭用例首次报
  `provider worker exited with status 1`，手工执行它生成的 worker 脚本一次，读到
  `ImportError: cannot import name 'provider_activity'`——worker 继承的是 venv 自带的
  `factory` 包而非本工作树，在生成脚本里 `sys.path.insert(0, <repo root>)` 后通过。

**未覆盖 / 交给 Codex。** 目标端回执格式只在本地真脚本 fake-target 上验证过，真实服务器
上的实际输出格式仍需确认（真实服务器属 M5）；本仓库后端全量未跑，本节只跑了上列集合；
`TARGET_ENV`/`VERB_ENV` 这两个新环境变量是本轮为"目标能造出可绑定回执"而加的约定，
真实目标脚本侧的适配尚未在真机上验证；A 项的进程组判活在 posix 上实测，非 posix 未覆盖。

## M3 手动 Issue 维护切片与独立调用边界

起点 `81151d4`（Codex 独立复核：原样 3 条边界探针 + M2 五集 **46 passed / exit 0**）。
真实 SDK 进程树、Linux、真实目标脚本回执仍归 M5，本节不宣称它们已支持。

**五条完成线（先列，逐条在下方回填实跑）**

1. 同键同内容的手动导入回到原任务/原执行/原回执，不重复付费也不开第二个工作区；
   同键不同内容是 409 等价冲突；reopen/update 形成显式新修订/后继，不覆盖旧证据；
   绑定的 repo 与完整 SHA 要能在真实 ProviderRequest / 执行工作副本里查到，不只在登记表。
2. 合成小型遗留仓库先有真实本地失败，再经执行端口修改、跑有意义的检查、导出补丁/构建物，
   并给出机器可读 + 人可读回执（原 Issue、基线、交付 commit/hash、适用约定版本、
   执行与测试结果、未验证项、部署层级）。单测/集成用显式 fake-model 隔离付费调用。
3. 独立 CLI/进程证明：不经 webuddy 网页，与适配器同一 Task 契约，
   创建 → 观察事件 → 补充/恢复 → 导出；CLI 必须以**真实子进程**跑，不是 `main(argv)` 单测。
   身份走既有受控身份或显式本地操作者边界，不通过 `body.actor='admin'` 获得权限。
4. waiting/failure/cancel/resume 如实映射，没有第二个无法解释的状态来源；
   重开进程后找到同一任务，费用/干预/构建物都在，且不产生额外派发；
   至少一条真实持久化恢复直接复用 M2。
5. 交给 M4 的稳定类型化接口与最小样本数据：步骤、阻塞原因、事件引用、证据适用性、
   实际能力来源、交付层级。本轮不改导航/大屏，不凭计时器编进度。

**文件归属（主会话独占共享状态/迁移/生命周期）**

| 文件 | 归属 | 说明 |
| --- | --- | --- |
| `factory/control/issue_maintenance.py` | 主会话 | 领域模块：任务身份、Issue 版本/来源、固定 repo+base SHA、约定版本、execution 引用、交付回执；模块自有表 |
| `factory/control/issue_maintenance_webuddy.py` | 主会话 | webuddy 适配器：把端口接到既有 Service/Store/execution，不新建执行器/预算/状态机 |
| `factory/control/maintenance_cli.py` | 子代理（接口稳定后） | 独立 CLI 入口，只调领域模块的 Task 契约 |
| `tests/test_issue_maintenance_core.py` | 主会话 | 完成线 1、4 |
| `tests/test_issue_maintenance_vertical.py` | 主会话 | 完成线 2（合成遗留仓库 + 真实失败 + 回执） |
| `tests/test_issue_maintenance_cli.py` | 子代理 | 完成线 3（真实子进程） |
| `docs/implementation/webuddy-operations/issue-maintenance.md` | 子代理 | 使用说明与合成 fixture 说明 |

**实跑回填（命令 / 退出码 / 证据版本 / 未验证项）**

落地两个提交：`0e7f006`（完成线 1、2、4、5）与 `59ad7a2`（完成线 3 + 一处静默降级修复）。

统一命令（本工作树自己没有 `.venv`，用 `v3-skills-icons` 的解释器；`-m "not smoke"`
排除真调模型的用例）：

```
PY=/Users/auntlee/workspace/.factory-worktrees/v3-skills-icons/.venv/bin/python
$PY -m pytest -q -p no:randomly -m "not smoke" \
  tests/test_issue_maintenance_core.py tests/test_issue_maintenance_vertical.py \
  tests/test_issue_maintenance_recovery.py tests/test_issue_maintenance_view_contract.py \
  tests/test_issue_maintenance_cli.py
```

结果 **70 passed / exit 0**；重跑后工作树干净（生成的 M4 样本逐字节一致）。

| 完成线 | 证据 | 未验证项 |
| --- | --- | --- |
| 1 | `test_issue_maintenance_core.py`（45 条）：计数式 fake 执行端口，重复导入的派发次数与工作区创建次数都是测出来的，不是描述的；同键改内容抛 `Conflict`（经 `app.py` 的 handler 即 409） | fake 端口不能证明跨进程存活，这一条由完成线 4 的真重启补上 |
| 2 | `test_issue_maintenance_vertical.py`（3 条）：合成遗留仓库先真实失败，`_FakeModel` 在**调用时刻**记录 HEAD 并断言等于基线，导出的补丁 `git am` 到新克隆后检查退出码 0，`diff_hash` 对得上导出字节 | 真实 provider 未接入；检查脚本是合成的，不代表任何客户仓库 |
| 3 | `test_issue_maintenance_cli.py`（13 条）：全部经 `subprocess.run` 起真实进程，文件里没有一处直接调 `main(argv)`；身份只认本机 OS 用户，`--operator root` 退出 2 且在打开数据库之前就拒绝（实测拒绝后目录仍为空）；补充→恢复各起一个子进程，从库里读回 `followup.pending` 与 `human.continued` | 未验证正在运行的 service 真的从队列取走 `create` 入队的那条运行；非 posix 未覆盖 |
| 4 | `test_issue_maintenance_recovery.py`（4 条）：关掉第一个协调器，用第二个 `Store`/`Service` 打开同一 `control.db`，跑 M2 既有 `recover()`；阻塞原因引用日志里真实存在的 `run.recovered` 事件序号；费用与未核销调用数（`unknown_cost_calls`）重开后仍在 | 真实崩溃（非 `svc.close()`）未验证；Linux 未覆盖 |
| 5 | `test_issue_maintenance_view_contract.py`（5 条）：样本由真实视图生成而非手写，递归扫描确认载荷里没有任何可用来画进度条的字段名；最后一步只在回执存在时才 `done`，两个方向都有断言 | M4 消费方尚未接入，不宣称界面已显示 |

本轮修掉的静默降级（`59ad7a2`）：适配器把缺失的 `resume`/`cancel` 钩子替换成返回
`None` 的 lambda，而 CLI 只为 `create` 接线，于是一次根本没到达生命周期的取消会打印
任务视图并退出 0。现在钩子无条件接线，缺省改为抛 `Conflict`；只有 `dispatch` 仍按
子命令门控，因为它未接线时是抛错而非静默成功。原有的 `cancel_requested` 断言在钩子
为空操作时同样通过 —— 三处新断言都用变异验证过会红。

**M2 遗留如实呈现（本单不追加大改）**

- 进程组判活只覆盖**留在组里**的后代；真实 SDK 现场的异常登记与脱组仍待确认。
  不把锁 / `killpg` 当成全平台恰好一次的证明。
- `TargetStore.last_checks` 新增的 `current` / `ttl_s` 仍在等 M4 消费方，
  不宣称用户页面已会显示过期。
- 未适配结构化回执的目标保持旧命令兼容；未知响应不自动重发。
  普通命令的 exit 0 与一次查询确认是两种不同证据。

### M3 补修 R1（Codex 对 `52c19db` 的两条 P1）

原复现 `tests/test_codex_m3_review.py` 逐字节照抄，断言未改；在 `52c19db` 上
**2 failed / 0.64s**。

**R1 建立客户端 ≠ 服务重启。** `Service.__init__` 里那条把 pending/running/
cancel_requested 的 `maintenance_jobs` 一律改写成 `interrupted` 的 UPDATE，是服务重启
恢复动作，却挂在构造函数上：独立 CLI 只是打开同一个 control.db 读任务列表，就把真实
服务正在跑的作业判死。清算动作移到 `Service.recover_maintenance_jobs()`，只由真正接管
的路径调用——`recovery.recover()` 在拿到 worker 锁之后调一次，`create_app()` 在构建
路由之前调一次。之所以不只放在 `lifespan`：`pack_router` 是在构建期按 job 状态对账
pack 任务的，清算必须排在它前面，否则上一进程随之而死的 pack 任务会永远停在「处理
中」。该方法只改非终态行，重复调用不会造出第二次中断。

**R2 登记与派发之间被中断，不再留下永久孤儿。** `create` 先 claim 幂等键再 submit，
中间失败会烧掉键，留下一个永远 `received`、没有执行可恢复/取消/对账的任务，之后每次
重试都走「已登记」分支回到同一条死记录。改为 claim 与绑定分离：`_bind_execution` 对
任何还没有执行引用的已登记任务补派发，已有引用的绝不再 submit（普通重复导入的行为不
变）。三个窗口靠既有机制收敛到同一次执行——`create_run` 按任务 id 去重，
`WebuddyExecution._dispatched` 改问 `DurableQueue.jobs(rid)`（新增只读方法）而不是靠
`created` 标志，这样「执行已建立但从没入队」与「已入队」才分得开。没有第二套调度，
没有计时器。

实跑（`v3-skills-icons/.venv/bin/python`，`-m "not smoke"`）：

```
tests/test_codex_m3_review.py                     2 passed / 0.67s   (原 2 failed)
5 个既有 maintenance 集 + 新集 + agent_service_review + pack_api_durability
  + capability_packs + restart_same_site                130 passed / 61.77s
control_app + auto_consume + operation_workflows + continuous_service
                                                       86 passed / 68.46s
```

新增 `tests/test_issue_maintenance_dispatch_recovery.py`（7 条）用真实 `Store`、真实
`create_run` 去重、真实 `DurableQueue`，不是 fake 端口重试：三个窗口各一条，断言重试
后维护类 run 恰好 1 条、队列条目恰好 1 条且 generation 仍为 1、派发计数不涨；另三条
锁住 R1 的两个方向（构造客户端不清算 / `recover()` 与 `create_app()` 仍清算）。回退产品
改动后，这 7 条中 6 条变红，仅「普通重复导入」一条本就是行为不变的守卫。

`tests/test_agent_service_review.py::test_restart_marks_unacknowledged_job_interrupted`
的行为断言保留不变，只把「重启」从「构造一个 Service」改写成显式的启动恢复调用，并加
了构造不清算的反向断言——原写法正是把构造当重启的那条耦合。

未验证项照旧：真实 SDK 进程树、Linux、真实目标脚本回执归 M5；CLI `create` 入队后由真实
运行中的 service 领取执行仍未实测，登记入队不等于执行完成。

### M3 补修 R2（Codex 对 `bae8e5c` 的锁竞争 P1）

原复现 `tests/test_codex_m3_startup_lock.py` 逐字节照抄，断言未改；在 `bae8e5c` 上
**1 failed / 0.63s**。

上一轮只把「构造 Service 就清算」搬成了「构造 Web App 就清算」：`create_app()` 在取得
worker lock 之前就调 `recover_maintenance_jobs()`，于是一个**竞争失败的第二个 Web 服务**
——还没进 lifespan、也没拿到锁——照样把活动服务的作业判成 `interrupted`，M2 已验收的
单一写入方边界仍然被破坏。

改为先认领再恢复：新增 `Service.claim_startup_recovery()`，先 `DurableQueue.acquire()`
（沿用既有那把锁，没有另造第二把），拿到才清算，拿不到就一行不改直接返回。清算仍在
`create_app()` 内、`pack_router` 对账之前完成，所以唯一真启动的服务顺序不变。

三处配套：
- **重复 acquire**：`acquire()` 本身在已持有时直接返回，因此赢家在 lifespan 里的
  `recover()` 不会和自己抢锁。
- **构建失败还锁**：`create_app` 拆成一层薄包装 + `_build_app`，只有本次调用真正取得的
  锁才在构建异常时 `release()`；调用方原本就持有的锁不动。否则一次失败的启动会让数据库
  被一个并没有在服务的进程占住，把下一次本可成功的启动挡掉。
- **竞争方不是静默降级**：它不清算，但也不能悄悄当第二个写入方启动——拒绝由 lifespan 的
  `recover()` 抛出，就是既有那句「已有工厂进程处理此数据库，不能重复启动执行器」。

实跑（`v3-skills-icons/.venv/bin/python`，`-m "not smoke"`）：

```
两条 Codex 探针 + 5 个既有 maintenance 集 + dispatch_recovery
  + agent_service_review + pack_api_durability + capability_packs
  + restart_same_site                                   135 passed / 63.41s
control_app + workbench_app + auto_consume + operation_workflows
  + continuous_service                                  100 passed / 73.33s
其余全部构建 app 的用例（admin_config_conversation / autonomous_service /
  project_discovery / requirement_analysis / runtime_execution /
  session_skill_auth / session_skills / session_skills_github）
                                                        135 passed / 90.66s
```

最后一组是因为 `create_app()` 现在会取锁，所有构建 app 的用例都属于直接受影响面，跑一次
确认没有把别的启动路径锁死。

`tests/test_issue_maintenance_dispatch_recovery.py` 增至 11 条，新增四条覆盖启动所有权：
竞争方一行不改且不持锁、竞争方明确收到「已有工厂进程」冲突、赢家可在 lifespan 再次
acquire、构建失败后锁被归还且后继进程能接管。回退产品改动到 `bae8e5c` 后，Codex 探针与
其中两条变红；另两条本就是新机制的守卫，如实记为守卫而非复现。

## 下一步

M3 补修已落地，候选分支 `codex/operations-20260921` 交 Codex 复核，未进入 M4、未部署。
真实 SDK 进程树、Linux、真实服务器与真实目标脚本回执仍归 M5。
`docs/implementation/webuddy-operations/issue-maintenance.md`（使用说明，规格里标为可选）
尚未写。
