# 代码来源与快速检索

本书根据同版本源码和测试整理。以下行号是 2026 年 9 月 30 日本地代码核对位置，后续改动会移动；路径和 symbol 比固定行号更可靠。

仓库为 [auntunt/unmanned-factory](https://github.com/auntunt/unmanned-factory)。本地增量尚未推送时，远端 main 不包含这些恢复修改，不能用远端页面代替本地交付的代码证据。

| 主题 | 文件 | 定位 |
| --- | --- | --- |
| 启动与认证 | `factory/control/app.py` | `def _build_app`，约第 99 行 |
| 服务调度 | `factory/control/service.py` | `def _dispatch`，约第 114 行 |
| 单写者锁 | `factory/control/autonomy.py` | `class DurableQueue`，约第 84 行 |
| 恢复阶段选择 | `factory/control/recovery.py` | `def _continuous_resume_stage`，约第 42 行 |
| 持续执行和检查点 | `factory/control/continuous.py` | `def execute_continuous`，约第 123 行 |
| 精确提交意图 | `factory/control/execution.py` | `def _commit_tree`，约第 555 行 |
| 检查身份 | `factory/control/evidence_identity.py` | `environment`，约第 12 行 |
| 最小执行环境 | `factory/harness/checkenv.py` | `def check_env`，约第 46 行 |
| 活动进程组证据 | `factory/control/provider_activity.py` | `def group_alive`，约第 74 行 |
| 数据库与事件 | `factory/control/store.py` | `class Store`，约第 50 行 |
| 用户与会话 | `factory/control/auth.py` | `class AuthStore`，约第 113 行 |
| 团队授权与账本 | `factory/control/governance.py` | `class Governance`，约第 32 行 |
| 持久运行配置 | `factory/control/runtime.py` | `class RuntimeSettings`，约第 153 行 |
| 本地诊断快照 | `factory/control/runtime_cli.py` | `def _make_settings`，约第 66 行 |
| 真实只读探针 | `factory/control/runtime_routes.py` | `def probe`，约第 121 行 |
| Provider 错误分类 | `factory/control/providers.py` | `def failure_metadata`，约第 98 行 |
| 部署私钥边界 | `factory/control/deploy_targets.py` | `def key_dir`，约第 47 行 |
| 成果归档 | `factory/control/deliverables.py` | `def snapshot`，约第 42 行 |
| 项目预算边界 | `factory/control/run_billing.py` | `def _remaining_dollar_budget`，约第 154 行 |
| 维护 CLI 的 OS 身份 | `factory/control/maintenance_cli.py` | `class LocalOperatorIdentity`，约第 56 行 |
| 隔离演练 | `scripts/preview_v3.py` | `def rehearsal_app`，约第 73 行 |
| 开发代理 | `frontend/vite.config.ts` | `proxy`，约第 4 行 |
| 前端命令 | `frontend/package.json` | `"scripts"`，约第 6 行 |
| 更新脚本 | `deploy/install-control.sh` | `--dry-run`，约第 12 行 |
| 正式入口配置示例 | `deploy/factory-control.service` | `ExecStart`，约第 17 行 |
| 现有 CI | `.github/workflows/ci.yml` | `isolation_canary`，约第 78 行 |
| 恢复专项测试 | `tests/test_continuous_check_recovery.py` | `def test_`，约第 41 行 |
| 提交恢复专项测试 | `tests/test_continuous_commit_recovery.py` | `def test_`，约第 35 行 |

## 检索方式

```sh
# 下列只读命令在仓库根执行
rg -n 'def _continuous_resume_stage' factory/control/recovery.py
rg -n 'commit_intent' factory/control/continuous.py factory/control/execution.py
rg -n 'FACTORY_' factory/control/app.py factory/control/runtime.py
```

机器可读索引：[source-map.json](source-map.json)。文档 check 验证索引的文件与 symbol 存在；它不能自动证明所有自然语言结论都正确，仍需代码评审。

## 相关深入文档

现有仓库中的 `docs/continuous-coding.md`、`docs/gateway-billing.md`、`docs/maintenance-subsystem/CONTRACT.md`、`docs/reviews/2026-09-30-continuous-check-recovery.md` 和 `docs/reviews/2026-09-30-commit-intent-recovery.md` 提供详细协议与测试记录。它们不在本 GitBook 内容根内，离线阅读时应从源码仓库对应路径查阅，避免生成会失效的跨根相对链接。
