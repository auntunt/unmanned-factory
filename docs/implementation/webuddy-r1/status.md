# webuddy-r1 状态

- 基线核对：2026-09-17，起始 f81622a；集成分支 v3-conversation-workspace 当前 b697494。
- T01：local_reviewed（探针；断点 B1/B2/B3）
- T02：local_reviewed（B1 定性为代码缺陷并修复：setting_sources=[] 剥离凭据源 + CLAUDE_CODE_* 宿主变量泄漏；两轮真实模型执行穿链，V1-01/02/04 本地段证据成立；已合并）
- T03：local_reviewed（followup pending/applied 安全节点消费；首轮「校验前标 applied」缺陷已修正为 collect→校验→update→mark；已合并）
- T04：local_reviewed（契约驱动工具面板 + 四类失败态；csv-quote-xml 真实全链实测 seatbelt 隔离通过，V1-07 非 MFD 实证；已合并）
- T06：local_reviewed（公司测试环境配置区；后端接线回归补齐；已合并）
- T05：local_reviewed（已合并 88cadc5）
- 集成检查：前端 tsc/build 通过；全量 vitest 348/348（连续 3 次；曾出现 1 次未复现的单测失败，已记录）；后端全量（uv run，88cadc5）2375+1 断言对齐后通过；前端 354/354。全部任务 ready_for_codex，交接见 codex-handoff.md。

## 模型核实（宿主转写元数据）
请求 model=sonnet 的所有子任务实际均为 claude-opus-4-6；宿主未生效模型指定。执行者不称为 Sonnet；按用户授权以可用模型推进，并行规模压在 4。

## 已知移交项（给 Codex）
1. providers setting_sources [] → ["user"] 行为变化：服务器需核对部署用户 ~/.claude 用户设置无非预期 hooks/权限（MCP 已被 strict_mcp_config 阻挡）。
2. 真实 GitHub bind→publish 与服务器部署/固定测试地址验证（V1-03 线上段）。
3. checks 命令被 exec 为路径的 Errno 2（既有验证段行为，另立任务，不在本轮）。
4. 真实 SDK runner 下 needs_human 安全节点自动消费 followup 的现场联测。

## r2 验收前修复批次（2026-09-17，基线 5018e7d）
Codex 初审：/Users/auntlee/Desktop/自动化harness构建/docs/webuddy-r1-handoff-intake-2026-09-17.md（ready_for_codex，未验收）。
- T07(A)：running——checks 命令契约（Errno 2）；执行端 argv 契约已定位 execution.py，查生成/存储层后最小修复 + 真实模型平台复验 counter。
- T08(B)：running——安全节点自动消费（runner 状态转换触发，禁手工 continue_run 冒充）+ 逐条 pending_id 徽标 + 完成/取消/崩溃方向。契约④。
- T09(C)：running——delivery_type 正常流程写入（需求分析层）+ 两页展示一致 + 走真实推导路径验证。契约⑤。
- T10(D)：running——setting_sources ["user"] 边界矩阵（隔离 HOME 实验，不动真实配置，不出凭据）；结论决定改或固定。
工作区：webuddy-r2-t07/t08/t09/t10，各自独占；RunWorkspace.tsx 由 T08（消息区）/T09（结果卡区）分区。
模型核实：四个执行者请求 sonnet，宿主转写元数据实际均 claude-opus-4-6（与 r1 一致的宿主限制，不冒称 Sonnet）。
测试口径更正（采纳 Codex）：r1 后端应表述为「88cadc5 全量一项失败，00a833e 修正后该文件定向复跑 9 项通过」，非最终 HEAD 一次完整全绿。

## r2 收尾（2026-09-18）
- T07：local_reviewed，已合并。现场实证：单字符串 checks 正确拆分执行，真实失败 exit=4 与重试成功 exit=0 均入状态（真实模型 run，$1.36）。
- T08：local_reviewed，已合并（含追加轮 c658c74+ae906a3）。联测暴露 fake 测试盲区：钩子在 _run except 内执行时本 run 仍在 active_jobs（_job finally 才 pop），自动消费现场必跳过；修复移钩子至 _job finally，全路径回归经变异验证（旧码红/新码绿）。现场自动接续未观察到：三次真实模型尝试均因账号节流、执行段超窗未到 needs_human（约 $3–4），现场确认列入 Codex 长任务验收。
- T09：local_reviewed，已合并（含 non_goals 负向信号修正）。现场实证：spec.auto_confirmed 实跑，delivery_type_inferred=cli。
- T10：local_reviewed，已合并。矩阵结论：["user"] 认证必需但连带加载 hooks/permissions/env；strict_mcp_config 只挡 MCP servers。不改代码，服务器 6 步检查清单为 79b7035 上线前置。
- 集成检查（60fb644）：前端 tsc/build 干净、全量 vitest 364/364；后端全量三次——第 1 次 1 failed（continuous restart 参数化用例）、第 2 次 1 failed（pack durability restart 用例）、第 3 次 2427 全绿；两条失败均为一次性、单跑/整文件/组合均过，未复现，列为观察项（重启形态、疑与负载时序相关），建议 Codex Linux 复验留意。
- 测试口径：以上按「每次运行的真实结果」分别记录，不合并表述为一次全绿。
