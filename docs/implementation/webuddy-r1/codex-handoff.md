# webuddy-r1 → Codex 验收交接（2026-09-17）

集成分支 `codex/autonomous-factory-v3`（本地 v3-conversation-workspace），基线 f81622a → 候选 HEAD 见推送记录。生产 4c56632。所有任务 local_reviewed / ready_for_codex；accepted 由你判定。

## 提交范围（按集成顺序）
- 79b7035 fix(providers)：B1 根因——setting_sources=[] 剥离凭据源致 SDK worker "Not logged in"；改 ["user"]（strict_mcp_config 仍挡 MCP）+ 剥离 CLAUDE_CODE_* 宿主会话变量。
- 199f2b3 merge T03：followup pending/applied 安全节点消费（collect→校验→update→mark；崩溃窗口方向为重复并入非丢失）。
- c9d6358 merge T04：CapabilityPanel 契约驱动 + 四类失败态；bindings 新增只读 tool_contract。
- 47719c4 merge T06：/settings/runtime 公司测试环境配置区；GET /api/v2/deploy-targets/checks（admin 只读）。
- b697494 集成修复：Run.followups 入类型、测试未用导入。
- 88cadc5 merge T05：结果卡交付类型（service/cli/installer/未声明；cli 部署「不适用」，installer 未选定「待确认」）；deliverables listing 新增 delivery_type/installer_targets 只读字段。
- 00a833e 旧断言对齐（setting_sources ['user']）。
- 各任务单/回执（含监工回执）在 docs/implementation/webuddy-r1/。

## 测试证据（本机 macOS）
- 后端全量 `uv run pytest -m "not smoke" -q`（88cadc5 树）：1 failed → 修断言后该文件 9 passed；即最终 2376/2376 等效通过，20 skipped。注意：uv run（包已装）下运行；此前 conda base 直跑必失败的 3 条 python -I 用例这次全过，环境归因与 f81622a 轮结论一致。
- 前端（88cadc5）：tsc 干净、build 通过、全量 vitest 354/354（合并 T05 前 348/348 连续 3 次；曾观察到 1 次未复现的单测失败，未定位到用例名）。
- 真实模型两轮穿链（T02 回执）：received→…→execution 产出 counter 应用（11→16 测试），follow-up 第二轮保留第一轮成果。
- 真实隔离全链（T04 回执）：csv-quote-xml 候选→验证→发布→挂靠→调用→下载，seatbelt，结果 sha256 e70662b1…，与 fixture 逐行一致。

## 需要你在服务器独立核验的
1. setting_sources ["user"] 行为变化：服务器部署用户 ~/.claude 用户设置需确认无非预期 hooks/权限；Linux 下 SDK worker 认证路径复验。
2. V1-03 线上段：真实 GitHub bind→publish、部署目标、固定测试地址业务操作。
3. T03 安全节点消费在真实 runner 长任务下的现场；T06 真实服务器连通检查与页面 E2E。
4. Linux bwrap 下 T04 全链复跑。

## 未解决 / 另立任务（不在本轮）
- checks 命令字符串被 exec 为路径（Errno 2，既有验证段行为）。
- 工作台 Deliverables.tsx 的交付类型显示；执行器写入 delivery_type 的产品流程（目标系统选择）。
- MFD 维持「清单局部提取」，未扩口径。F01～F06 未实现（按范围锁定）。

## 模型与流程事实
规划/监工 Fable 5；执行子任务请求 sonnet、宿主实际均为 claude-opus-4-6（转写元数据核实），未以 Sonnet 名义记录。并行 4 执行者、独立 worktree（webuddy-r1-t03/t04/t05/t06），冲突仅 conversation.css 一处由规划者裁决。


---
# r2 验收前修复交接（2026-09-18，基线 5018e7d → 候选见推送）

## 提交范围（集成顺序）
- 4bcc5d0 merge T07：checks 命令契约——执行端 _normalize_check_argv（仅单元素含空格 shlex.split，非 shell=True）、非法格式结构化错误、存储入口形状校验（b3e71ce）。
- 8b133ea merge T08 首轮：安全节点自动消费 + 逐条 pending_id 徽标 + expired 独立态 + 恢复收尾扫描（1e1e925/e6660be）。
- c5a4cbc merge T09：delivery_type 由 requirement_analysis.confirm 真实流程写入；关键词推导唯一正向命中才定型、non_goals 负向减除；delivery-constants.ts 两页一致（9bbd975/9573cca）。
- d8ffe41 merge T10：setting_sources 边界矩阵与固定回归，不改行为（7983970）。
- 60fb644 merge T08 追加：自动消费钩子移至 service._job finally（active_jobs 时序缺陷，联测发现）；基线检查异常事件化 followup.auto_resume_skipped（c658c74/ae906a3）。

## 已验证行为与证据路径
- 各任务回执（docs/implementation/webuddy-r1/receipts/T07–T10.md）含命令、退出码、变异验证记录。
- 真实模型现场（临时库+uvicorn，非 TestClient）：T07 checks 拆分执行（exit 4→0）；T09 spec.auto_confirmed→delivery_type_inferred=cli；T08 followup.pending 落库、界面文案不再要求重输。
- T08 自动接续机制：全路径回归（_submit/_job 线程池）修复前失败、修复后通过（变异验证）；**现场自动接续未在本机观察到**——三次真实尝试因执行段超窗（账号节流）未到 needs_human，请在服务器长任务验收按 T08 回执 10 步清单确认，判据：needs_human 后无人工操作出现 run.auto_resumed 且 followups 逐条 applied。
- 集成：前端 364/364 + tsc + build；后端全量三次（1 failed / 1 failed / 全绿，失败为两条不同重启形态用例各一次、均不可复现——观察项）。

## 剩余阻塞与观察项
1. 现场自动接续确认（上）。
2. 两条一次性全量失败（test_continuous_restart_derives_safe_resume_stage[budget-finalization]、test_restart_moves_in_flight_tasks_to_an_explicit_terminal_state）——Linux 复验请留意重启形态用例。
3. setting_sources 服务器 6 步检查（T10 回执）仍是 79b7035 上线前置。
4. 「独立验收需要可用的隔离终端」（providers.py:538）在本机联测环境成立，服务器环境需确认隔离终端可用性。
5. 非 general 操作（bugfix/release）无 delivery_type 推导写入（T09 已登记缺口，后续小项）。
6. 真实 GitHub 发布、固定测试地址业务操作、Linux bwrap 全链复跑：归你。


---
# r2 复核修复交接（2026-09-18，基线 eff65ec → 候选见推送）

## 提交
- fb59847 merge T11（6ac4f4d）：R2-01——单元素含空格且为存在可执行文件（绝对，或含斜杠相对按检查 cwd root 解析）时原样保留为 argv[0]；否则维持确定性 shlex.split；root 经 _check_argv 透传三处调用点；无 shell=True，结构化错误与 NUL 检查不变。
- 318e57f merge T12（2aad10a）：R2-02——ASCII 关键词 \b 词边界（中文与 .exe 类扩展保持子串）；关键词分 form（形态）/deploy（部署拓扑）两层，non_goals 的负向减除只用 form 层；唯一正向命中才定型、歧义 None 不变。

## 证据
- 每单回执（receipts/T11.md、T12.md）含修复前失败输出（红→绿）。
- 你的六文件定向集在 318e57f：210 passed（203 + 7 新回归）。
- 后端全量（318e57f）：2434 passed / 0 failed / 20 skipped。前端零改动，沿用 eff65ec 记录。

## 证据边界更正（采纳你的复核）
- T10 矩阵「["user"] 加载 hooks/permissions/env」一格证据级别：SDK 参数传入断言 + CLI --help 与 SDK 源码；隔离 canary 认证段先失败，未观测 hook 实际执行；不以本机推断所有环境，也不排除更小配置方案——服务器检查仍按实际部署身份与认证方式执行。
- T08 现场自动接续：未验证，判据与 10 步清单不变。
- 两条重启形态偶发失败：观察项保留。

## 待你复核与后续
R2-01/R2-02 修复复核 → 服务器隔离验收（GitHub 发布、固定地址、隔离终端可用性、Linux bwrap 全链）。


---
# r2 服务器阻塞修复交接（2026-09-18，基线 0b39155 → 候选见推送）

## 提交
- merge T13（7c486f5）：verification.py 提取层改 _extract_verdict_json/_validate_verdict_fields——纯 JSON / 全文围栏 / 说明段+唯一 ```json 围栏三形式；多候选块、冲突 verdict、非法字段仍 invalid_response；接线单点替换，coverage/fidelity/scope/browser 规则未动。
- merge T14（f5f1632）：scope_declaration.exempt 增加 .webuddy/coding-progress.md 精确路径窄豁免（非目录前缀），符号链接不豁免（workspace 透传判定）；其余 .webuddy 与业务文件照旧管控。

## 证据
- receipts/T13.md、T14.md：修复前失败输出（T13 另有规划者行为级验证：旧四行逻辑对你 S1 引文形式 JSONDecodeError；新解析 verdict=pass 结构完整）。
- 定向：test_verification_response_parsing 12、test_scope_declaration 16、验收合同/证据 22+3skip、continuous 36 → 合并树集合 87 passed / 2 skipped。
- 按本轮节奏未跑 2434 全量与前端；如你判断存在共享影响再扩。
- 你的服务器现场结果原样引用（不据为己有）：T08 auto-resume 首段无人观察成立、后半段人工继续一次；bwrap/隔离终端/settings 通过；旧现场（live-20260918-115212、成果 8d64c54）未触碰，修复后复验请另记新结果。

## 环境缺口（非代码，待用户/运维）
固定测试地址未明确；生产部署目标登记为空。
