# 验收证据说明

截图、事件与补丁均来自 2026-09-23 的真实运行（合成仓库，claude-sonnet-5），按原样保留，不回改。

## 已知的历史错误（由 Codex 初审 baa8b62 指出，后续提交已修）
- `v3-delivery.patch` **没有**保留 `v2-delivery.patch` 新增的 `test_total_multiple_quantity`：当时后续反馈
  从项目旧基线重做，而不是在 v2 交付上继续。修复后后续反馈在上一版交付的工作副本上继续，并导出
  增量/累积两份补丁及其适用基线（确定性测试
  `test_feedback_after_delivery_keeps_the_delivered_change_and_states_patch_basis`，未再付费重跑模型）。
- `key-events.json` 与当时回执里的任务 `synthetic: false`：合成仓库当时没有显式标记渠道。修复后以
  登记/接入来源的显式声明贯穿；验收仓库已在修复后标记为合成，只影响之后接收的需求，历史任务不改写。
- `01/02-*.png` 画布里同一任务的第二个补丁与下一任务相邻；按任务组分配行高后见 `09-canvas-grouped-layout.png`。
