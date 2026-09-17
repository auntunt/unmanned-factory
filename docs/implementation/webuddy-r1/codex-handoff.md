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
