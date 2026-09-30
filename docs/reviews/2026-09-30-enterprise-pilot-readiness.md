# 单组织企业试点：可验证的就绪门槛

本轮面向单组织、可信团队部署。它不是独立租户隔离、多区域可用性或企业合规认证承诺，不自动发布、合并或修改生产环境。

## 已修复的具体风险

- 新建认证、控制和旧审计数据库在默认 umask 下可能为 0644：现在新文件 0600、新建祖先目录 0700；不改现有权限或进程全局 umask
- 嵌套审计对象的 password、DEPLOY_PASSWORD、clientSecret 等字段可漏出：共享结构化敏感键规则遮蔽值，保留用量计数和会话引用
- Caddy 示例强制 HTTP 与应用 HTTPS 登录约束冲突：示例改为域名自动 TLS，额外 basic auth 明确可选，不引导把密码写到 shell 历史

这些是代码/模板修复，不表示已替现有服务器修改权限、证书或代理配置。

## 只读预检

`factory-runtime preflight --json` 检查本机部署前提。状态为 passed、blocked 或 unknown，退出码分别为 0、1、2。它不写生产状态、不探测真实模型、不把“安装了 sandbox 二进制”冒充“隔离可用”。前端项只校验 HTML 的本地脚本/样式引用存在，不替代真实浏览器交互。

完整上线门槛依然包括对应提交的完整 CI、实际供应商调用、独立验收、用户角色/工作流浏览器验收、外部 TLS，以及受控备份恢复演练。预检将无法验证的这些项目单独标为 unknown。

## 合成备份恢复演练

`tests/test_enterprise_restore_drill.py` 创建临时账户、项目、授权、运行和审计事件，停写后调用既有 SQLite 快照工具，再复制到另一个临时目录恢复。核对账户登录、项目数、run ID/状态、事件内容/ID、成员权限边界、秘密脱敏及原备份未改变。

此测试不含生产数据、不启动后台执行器、不调用模型，也没有把数据库快照当成包括 Git/worktree/文件成果/密钥的完整备份。它明确验证旧会话会随 users.db 恢复；正式恢复必须由授权人员处理会话风险。

## 验证记录必须按证据分类

- 单元/服务与故障注入：真实本地代码、SQLite、Git，供应商调用使用测试替身
- UI E2E：真实浏览器/HTTP/后端流程，预览中的模型仍为合成响应
- 模型端到端：只有实际受授权供应商请求和独立验收才能记通过
- 生产验收：必须绑定真实目标、证书、服务账户、备份文件与运维负责人

不能把前三项中的局部成功合并成未实际完成的生产验收。

## 本轮本地结果

- 三项硬化新增最初回归：16 failed / 4 passed，证实修复前泄漏与权限问题
- 最终下列相关集合：253 passed，1 条既有 Starlette 依赖弃用警告
- `compileall`、`git diff --check` 通过
- 实际 `preflight` 对临时合成数据库、当前构建前端运行，按预期退出 1：真实隔离 canary 因 NETLINK_ROUTE 权限受限而 blocked；没有更改权限或退回不隔离执行

```sh
.venv/bin/python -m pytest tests/test_enterprise_hardening.py tests/test_deployment_preflight.py tests/test_enterprise_restore_drill.py tests/test_redact.py tests/test_control_auth.py tests/test_audit_store.py tests/test_runtime_cli.py tests/test_control_app.py tests/test_org_governance.py tests/test_store_atomicity.py tests/test_audit_archive.py tests/test_app_route_contract.py tests/test_base_install.py tests/test_continuous_execution.py tests/test_continuous_commit_recovery.py tests/test_continuous_check_recovery.py docs/handbook/tools/test_snapshot.py docs/handbook/tools/test_handbook.py -q --tb=short
```

完整后端/浏览器门禁以该发布候选提交的 CI 结果为准；此处没有声称本地受限环境跑过完整隔离 CI。
