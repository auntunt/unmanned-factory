# 文件所有权表（本轮冻结）

2026-09-21｜分支 `codex/enterprise-plugins`｜基线 `b854a41`（= 已核对候选，远端 tip）。

集成者一人负责公共契约；三个场景执行者只改各自列内文件。冲突不靠覆盖解决。

## 集成者（本会话，Opus 5）

| 文件 | 变更 |
|---|---|
| `factory/control/plugins.py` | 新增：插件声明、可用性状态机、服务侧闸门 |
| `factory/control/plugin_routes.py` | 新增：`/api/v2/plugins` 读取与管理员启停 |
| `factory/control/issue_maintenance_webuddy.py` | `tasks_for` 收口为唯一装配点，挂闸门 |
| `factory/control/maintenance_cli.py` | 改为调用同一装配点，不再自建端口 |
| `factory/control/maintenance_routes.py` | 可用性视图；`approve` 直连 svc 的那条路补闸门 |
| `factory/control/app.py` | 显式注册插件路由 |
| `frontend/src/lib/api.ts` | 插件读写客户端 |
| `frontend/src/conversation/nav-config.ts`、`App.tsx` | 设置页新增「业务插件」 |
| `frontend/src/workbench/PluginsSettings*.tsx` | 新增管理员启停界面 |
| `docs/implementation/webuddy-enterprise-plugins/*` | 本目录 |

## 场景执行者（Sonnet 子会话）

| 场景 | 可改文件 | 明确禁改 |
|---|---|---|
| 01 信创化改造 | `factory/control/legacy_modernization*.py`、`builtin_packs/packs/legacy-modernization/**`、`frontend/src/workbench/Modernization*.tsx` | 上表集成者文件 |
| 02 自动化运维 | `factory/control/issue_maintenance.py`、`builtin_packs/packs/issue-maintenance/**`、`frontend/src/workbench/Maintenance*.tsx`、`tests/test_issue_maintenance_*.py` | `maintenance_routes.py`、`issue_maintenance_webuddy.py`、`plugins.py` |
| 03 接口适配 | `factory/control/api_adaptation*.py`、`builtin_packs/packs/api-adaptation/**`、`frontend/src/workbench/Adaptation*.tsx` | 上表集成者文件 |

`factory/control/app.py`、`store.py`、`service.py`、`governance.py` 只由集成者改。
