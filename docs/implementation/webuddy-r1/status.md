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
