# 聊天两项修复独立验收（2026-09-19）

候选 `ebf0a64394051d92d2696e6cbc03220874648769`；GitHub 远端 codex/autonomous-factory-v3 同 SHA。前置代码 `f5b238c`，两次修改 `51e518a`、`ebf0a64`。

## 结论

新发起普通无项目 do 聊天的 pending 生命周期与成员入口两项通过本轮独立验收。生产尚未更新，仍为 `4c56632d`，factory-web.service active。未将本结论扩大为整个平台验收。

## 本次实际执行

- macOS 定向后端：23 passed / 10.63s。含原始独立成员复现、聊天生命周期、成员正反向权限、admin-config 竞态、路由契约。
- Linux 同一定向集：23 passed / 28.03s。
- 前端 AgentChatPage.test.tsx：10 passed。含停止 typing 与轮询。前端产品代码未修改，本轮未重复全量与构建。
- 真实 Linux HTTP + SDK 模型：新建临时库及管理员、member 身份，使用服务现有 Claude 模型配置。普通成员创建 do 会话 201、发送消息 201；跨用户读取、管理员配置入口、maintain 创建均 403。
- 真实模型通过 export 生成带独特标记的 Markdown；成员下载 HTTP 200，标记匹配，SHA256 见 live-acceptance.json。
- job completed；同一 job 只有一条 assistant completed 消息，活动 pending 数为 0。
- 真正停止并重启隔离服务后，以同一 member 登录：消息保持原样、文件仍有记录、没有 pending。隔离进程最终已退出。

## 证据与范围

现场脚本 live_member_probe.py；脱敏结果 live-acceptance.json。服务位于 /home/ubuntu/releases/audit-next-ebf0a64，仅环回端口 18921；独立数据目录见 JSON。未修改生产配置/业务数据库、未部署生产、未新建 GitHub 仓库。本轮只读核对远端提交。

现有边界：无 boot_id 的旧 pending 不自动清理；maintain 整理链路同类问题不在本轮修复；boot_id 恢复仅适用于当前单进程形态，不能据此批准多 worker 部署。真实模型取消与中途强制重启未在此次实测覆盖；失败/取消及旧进程标记由定向测试覆盖。
