# 提交成功但检查点未落盘：精确意图恢复

## 问题与边界

旧路径在 `git commit` 成功之后才更新 `workspace_guard` 并保存完成检查点。如果中间进程退出，重启时看到新 HEAD 和旧 guard，只能拒绝恢复。拒绝是安全的，但已完成任务会被卡住。

现在只有 continuous 专用路径启用提交意图。旧 DAG 调用 `_commit_tree` 的默认行为保留，且不会改变发布、合并、权限和计费规则。

## 协议

1. 配置检查整组通过，保存 `finalization_checkpoint`
2. 再次核对源码签名，记录文件内容和模式的快照
3. 只 stage 已核验路径，核对 staged 路径集合并获得 tree
4. 用 `commit-tree` 创建确定的提交对象，此时不推进分支
5. 保存 `commit_intent`：版本、精确 expected SHA、单一 parent、tree、完整 branch ref、路径、原签名与 staged 签名
6. 只有检查点保存成功且未取消，才执行 `update-ref branch expected parent`，以比较并交换方式推进专用分支
7. 核对实际 HEAD/ref 和其他 Git 元数据，保存完成检查点，移除临时意图

提交消息含唯一意图标记。提交身份保持 `Factory <factory@localhost>`；原路径就禁用 Git hooks，新的对象创建不会额外放开它们。`commit.gpgSign=true` 显式传入 `-S`，沿用现有 signing key/GPG 配置；签名失败不能产生一个被当成成功的无签名提交。

## 重启判定

HEAD 与旧 guard 不一致时，只有以下条件全部满足才接纳：

- 存在版本合法的持久提交意图和完整成功检查证据
- 当前 HEAD **恰为 expected SHA**，不是仅仅 parent/tree 看起来相同
- 分支、原父提交及所有非 HEAD guard 字段保持一致
- 用 `--no-replace-objects cat-file -p` 读取的原始提交对象具有预期的单一 parent 和 tree，不依赖可被 graft 改写的 ancestry 查询
- 工作区干净，检查的路径/源码签名与意图、完整检查点一致

接纳后仍重新核验检查身份，失效项重跑；独立验收照常继续。检查重核期间再次退出，旧 guard 和原意图仍可识别，下一次不会丢失恢复入口。明确的新需求则回到编码模型，不能被“只恢复验收”的捷径吞掉。

意图已保存但 CAS 前退出时，HEAD 尚在 parent。只有 staged tree 和 staged 签名精确一致才调整阶段恢复依据，并重新验证检查，不把 staged 状态误当成已提交状态。

## 取消和其他写入者

取消后可以保留文件修改、回退本次专用分支提交。但只能回退原 parent 或精确 intended commit，且符号分支必须仍是原分支。发现其他提交或分支变化时拒绝回滚，不能通过恢复流程覆盖其他写入者。

## 尚未覆盖的窗口

`git add` 已完成，但提交意图尚未落盘时的硬崩溃，仍可能因新增文件在旧工作签名中的表示改变而拒绝自动恢复。此时 HEAD 未推进、源码保留，必须核对现场；不能宣称任意 finalization 崩溃都已自动恢复。普通异常走保留未提交修改的回滚，不等同于此硬崩溃窗口。

下一步可以引入版本化、与 staging 无关的源码快照身份，统一检查、提交准备和恢复对“同一源码”的定义，而不是继续为每个窗口增加特殊条件。

## 验证

`tests/test_continuous_commit_recovery.py` 使用真实临时 Git 仓库，覆盖提交后退出、CAS 前退出、重复恢复、重核时二次退出、新需求继续编码、不同 SHA 的同 parent/tree 提交、外来追加提交、脏源码、分支/配置变化、意图或检查点损坏、环境变更、持久化失败、签名失败、取消及外来写入者保护。

不调用真实模型，也不推送远端。完整 CI 仍需要可用的 bubblewrap 网络命名空间；本云端隔离前置探针失败，不能据相关测试通过宣称全套 CI 或生产部署已验证。

本次最终聚合命令（235 passed，1 条既有依赖弃用警告）：

```sh
.venv/bin/python -m pytest tests/test_continuous_check_recovery.py tests/test_check_environment_identity.py tests/test_continuous_execution.py tests/test_continuous_service.py tests/test_continuous_migration.py tests/test_evidence_reuse_scope.py tests/test_verification_evidence.py tests/test_command_evidence.py tests/test_checkenv.py tests/test_continuous_commit_recovery.py tests/test_effective_contract.py tests/test_control_execution.py tests/test_execution_deadline.py -q --tb=short
```

独立复核另运行提交恢复文件：23 passed；`git diff --check` 和修改模块 `compileall` 通过。旧窗口回归在修改前明确失败于 `continuous recovery Git metadata changed`，修复后通过。
