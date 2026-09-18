# webuddy r2 验收材料索引（给 Codex）

候选：分支 codex/autonomous-factory-v3，提交 7635ab6（工作区干净，与远端一致）。
关键提交均已含于候选：79b7035（B1 凭据）、1e1e925/e6660be/ae906a3（T08 三轮）、
b3e71ce/6ac4f4d（T07+R2-01）、9bbd975/9573cca/2aad10a（T09+R2-02）、7983970（T10）。

## 测试↔提交对照（真实结果，不合并表述）
| 树 | 检查 | 结果 |
|---|---|---|
| 88cadc5 | 后端全量 | 1 failed（setting_sources 旧断言）→ 00a833e 后该文件 9 passed |
| 88cadc5 | 前端全量+tsc+build | 354/354 通过 |
| 60fb644 | 后端全量 ×3 | 1 failed（continuous restart）/ 1 failed（pack durability restart）/ 2427 全绿；两条失败一次性不可复现，原始记录保留 |
| 60fb644 | 前端全量+tsc+build | 364/364 通过 |
| 318e57f | 六文件定向集（同你复核用） | 210 passed |
| 318e57f | 后端全量 ×1 | 2434 passed / 0 failed |
| 318e57f→7635ab6 | 仅 docs 变化 | 引用上行证据，未重跑 |

## R2-01 / R2-02 复现与修复证据
- 回执（含修复前失败输出）：../receipts/T11.md、../receipts/T12.md
- 最小复现即测试本身：tests/test_control_execution.py::TestCheckArgvSpacedPaths（真实子进程）、
  tests/test_control_deliverables.py::test_english_word_boundary_*、test_deploy_non_goal_does_not_negate_service
- 复跑：`uv run pytest tests/test_control_execution.py tests/test_control_deliverables.py -q`

## 真实链路（开发→平台检查→发布→固定地址→继续修改）
- 本机已证段：live-chain-probe.py 首次成功运行（T07 回执）：checks 单字符串格式
  真实模型穿链 check exit=0；另一次完整记录（handoff r2 节）：exit 4→重试→0。
- 未验证段（归你）：真实 GitHub bind→publish、固定测试地址业务操作、同地址更新。

## T08 自动接续判据（现场未验证）
运行中 POST /follow-up（响应 queued:true）→ 任务到 needs_human 后**无任何人工操作**：
事件流出现 run.auto_resumed{pending_ids}，GET run 的 followups 逐条 applied:true；
正常完成→未消费项 expired；取消→pending 保留且不恢复。10 步清单见 ../receipts/T08.md。

## 服务身份配置检查（79b7035 上线前置）
6 步清单见 ../receipts/T10.md（hooks/permissions/env/plugins 四键 + strict_mcp_config 边界）。
隔离终端可用性（providers.py:538）与 Linux bwrap 全链复跑归你。

## 问题分级
阻塞项（服务器验收范围）：T08 现场自动接续确认；服务身份 settings 检查；隔离终端可用性；
真实 GitHub/固定地址链路；Linux bwrap 全链。
非阻塞后续清单：两条一次性重启形态全量失败（原始记录见上表，不再无目标重跑）；
Deliverables.tsx 之外的显示细节；非 general 操作无 delivery_type 推导；MFD 维持局部提取口径。
