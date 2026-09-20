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
| M0 费用候选验收边界收口 | 完成 | 见下 |
| M1 长任务有效修订 / 原子干预回执 / 同快照验收 | 完成 | 见下 |
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

未验证：没跑全量，没做变异运动（任务明确限定只跑复现与格式直接相关集）。
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

## 下一步

M0 + M1 可审候选已交，本会话停止写入等 Codex 审查。M2（断点恢复、证据适用性、
外部动作意图）未开始。
