# 持续任务检查阶段的崩溃恢复

## 范围

基线：`main`，`5ff1f4c530cecd4b60695e6410ba3a310db51a94`。
沿用现有单工作区持续编码、持久事件、独立验收和 Git 边界，不改为固定多 Agent 流水线，不改变额度/计费策略，不改旧 CLI 引擎。

## 已确认的问题

此前 `verify_changed_tree` 在整组配置检查结束后才将结果保存到可恢复工件。即使第一项检查已通过，第二项运行期间服务退出，最新检查点仍可能是编码结束前的状态。自动恢复无法识别“编码已结束，检查未完成”，会重新调用编码并重新获得已有证据。

## 修改

- 编码结束、运行第一项检查前保存 `checks_checkpoint`
- 每项检查完成后先核对原有源码路径/内容签名和 Git 边界，再保存结果
- `checks` 恢复阶段只执行本地检查与归档，不调用编码模型
- 复用前重新计算检查身份；缺项或身份变化的检查重跑
- 执行环境身份从部分变量名单改为实际脱敏执行环境的完整摘要；CI、时区、区域设置等变化也会使旧结果失效，检查点不保存原始环境值
- 检查身份升级为 v2，旧指纹自动按未覆盖处理并重跑
- 失败、超时、取消的检查不能变成通过的证据
- `finalization_checkpoint` 仍只在整组通过后产生；检查进度不代表交付通过
- Service 恢复阶段识别新状态，保留独立验收流程

检查本身不具备通用 exactly-once 保证。若命令执行完、证据落盘前退出，仍会再次执行该命令。因此配置检查应是可安全重试的验证，不能夹带发布、收费等外部副作用。

## 回归覆盖

`tests/test_continuous_check_recovery.py` 用真实临时 Git 工作区和真实 Python 检查，再通过 `SystemExit` 模拟协调进程退出，覆盖首项/中途检查崩溃、不重复编码、不重复已通过检查、源码改变拒绝恢复、命令身份改变重跑，以及成功退出但修改源码的检查不能成为可复用证据。

Service 回归验证自动重启阶段识别、空白继续与新需求的区别，以及本地恢复不分配新的编码调用预算。

## 后续提交意图恢复

如果 `_commit_tree` 已成功推进任务分支，而新的 `workspace_guard` 与提交后检查点尚未持久化就退出，恢复会因 HEAD 与原有 Git 元数据不一致而拒绝。该行为是安全关闭，不会把未知提交误当成已验收成果，但会阻断自动恢复。

第二阶段已增加精确 expected SHA 的提交意图和 CAS 分支推进，详见 [提交意图恢复协议](2026-09-30-commit-intent-recovery.md)。依然拒绝外来 HEAD、额外父提交、不同 tree 和未完成检查；不是“忽略 HEAD 改变”。该协议也明确记录尚未覆盖的提交准备早期硬崩溃窗口。

## 验证边界

相关单元/服务测试可以在云端运行。仓库完整 CI 的隔离前置探针在本环境失败：`bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted`。没有绕过隔离，也不据此声明完整后端、浏览器或容器 CI 通过。真实模型端到端调用未运行。

验证命令与结果：

```sh
.venv/bin/python -m pytest tests/test_continuous_check_recovery.py tests/test_check_environment_identity.py tests/test_continuous_execution.py tests/test_continuous_service.py tests/test_continuous_migration.py tests/test_evidence_reuse_scope.py tests/test_verification_evidence.py tests/test_command_evidence.py tests/test_checkenv.py -q
# 133 passed（1 条既有依赖弃用警告）
.venv/bin/python -m pytest tests/test_base_install.py -q
# 1 passed（安装全部 extras 后的导入检查，不代替 CI 的最小依赖环境检查）
git diff --check
# 通过
```
