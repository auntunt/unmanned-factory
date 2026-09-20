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
| M2 断点恢复、证据适用性、外部动作意图 | 未开始 | — |
| M3 人工 Issue 纵向流程 | 未开始 | — |
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

## 下一步

M0 独立定向通过。M1 的两条边界已转绿，等 Codex 复核后才算完成——总表暂不改成全完成。
M2（断点恢复、证据适用性、外部动作意图）未开始，本轮不做。
