# 企业治理 v1 状态（2026-09-23）

**已完成的是最小纵切，不是完整的企业治理。** 分支 `codex/enterprise-governance-v1`，从已上线的 `de4df07` 起步；未合并 main，未部署，未改动生产身份和权限。

## 提交
| SHA | 内容 |
|---|---|
| `90e9f46` | 选择性摘取 `b226235`（README / .gitignore / 设计文档三文件，原提交基于旧的 v3 分支，不能直接合并） |
| `77c8911` | 组织树、项目归属、负责人只读管理范围（`/api/v5`）；README 第 5 行改为区分“已实现”和“方向”；“役位”改为“岗位” |
| `bdf4876` | 成员读取按项目分配收窄（旧 /v2 /v3 /v4 的项目与运行入口）；管理摘要不再回退到请求原文；管理首页、项目下钻、组织与授权页（页面由 Sonnet 子代理编写） |
| `ad5ef07` | 管理面的导航和面包屑修正；契约写明收窄范围与例外；截图和回执 |

## 已做
- 后端：`factory/control/org_governance.py`、`org_routes.py`；在 `governance.py` 增加读取规则；新增独立中间件 `member_read_scope`（`app.py`）；`/api/v2/projects`、`/api/v2/runs`、`/api/v3/overview`、`/api/v3/team` 过滤可读项目。`boundary` 中间件没有改。
- 前端：`frontend/src/management/*`（管理首页、项目下钻、组织与授权），AppShell 在有管理范围时显示“管理”入口，nav-config 增加 management 路由解析。
- 契约：`CONTRACT.md`，包括覆盖了哪些旧入口、还剩哪些明确例外。

## 命令结果
- `pytest tests/test_org_governance.py`：9 passed。覆盖子树可见性、手改 ID 返回 404、不能自己授权或提权、查看不等于执行、移动节点和撤销在下一次请求生效、循环/非法父节点/不存在对象、成员和管理员原有行为、审计只追加且按范围显示、未知费用不当成 0、旧入口不泄露其他部门、摘要不回显请求原文。
- 受影响的授权测试（org / team_governance / control_auth / session_skill_auth / member_daily_chat_access / maintenance_subsystem）：59 passed。
- 其余创建 member 的 29 个测试文件：524 passed, 1 skipped（用来确认成员读取收窄后没有引起退化）。
- `tests/test_app_route_contract.py`：2 failed，**在 de4df07 基线上同样失败**（中间件哈希在维护子系统加入 `maintenance_action` 时就已过期）。本轮没有改 `boundary`，也没有更新哈希。
- 前端：`tsc --noEmit` 通过；`vitest src/conversation src/management` 11 个文件、114 个测试通过；`npm run build` 通过（加了导航修正后重建了一次，一共两次构建）。
- 未跑全量测试，没做变异测试，没调用付费模型。

## 真实服务验收（合成数据，隔离库）
本地 `factory-web serve --port 8811`，数据目录在会话 scratchpad，工作区里只有 4 个合成 git 仓库。账号 gov-admin / leader-rnd / member-sales 全部是合成的。运行记录由 `evidence/seed-synthetic-data.py` 直接写入库中（标记 `synthetic`，不经过模型，不派发）。组织树、项目绑定、授权和撤销**都在页面上完成**。流程和截图：
1. 管理员建立 示例集团 → 研发部（→ 前端组）/ 销售部，绑定 3 个项目，legacy-tool 保持未归属，授权 leader-rnd 查看研发部。把研发部移到前端组之下时，页面显示“会形成循环”（01）。
2. 管理员总览：全部范围，包括未归属项目（02）。
3. 负责人：只看到研发部和前端组两个项目；用量只统计本范围（$1.25，另有 1 次费用未知，不包含销售部的 $9.99）；审计按范围过滤（03、04）；待处理只有提示，没有批准按钮（05）；打开销售部项目页面显示“范围不存在或你无权查看”（06）。
4. `receipt-leader-boundary.json`：23 个探针。管理接口越界返回 404；旧 /v2 /v3 /v4 的项目、运行、events、conversation、export、带 `project_id` 的查询全部 403（本部门的原始对话也是 403）；列表和聚合 200，且不含销售部数据；批准、自授权、改绑定都是 403。
5. 成员：没有管理入口（07），打开 /management 显示“你没有管理查看范围”（08）；`receipt-member-workbench.json` 显示已分配项目的列表、详情、对话、events 都是 200，未分配项目的运行 403。
6. 管理员在页面上撤销授权（09），负责人的下一次请求就被拒绝，导航里的管理入口也消失（10，`receipt-after-revoke.json`）。
7. 手机宽度 390：管理首页可用（11）。

## 未验证 / 限制
- 仍然开放的工作区级资产：能力资产、职能体/模块、职能包本体、插件审计、运行时与模型配置（清单见 CONTRACT）。
- 被拒绝的组织操作（例如循环、越权）没有写入审计，审计只记录成功的变更。
- 没有委派、审批流、预算分配、策略引擎，也不支持一个项目属于多个组织；管理视图没有分页（项目下钻最多 100 条）。
- 成员读取收窄是**行为变化**：以前成员能看到未分配项目的运行和对话，现在不能。升级前需要确认成员都有正确的项目分配，也就是 `/api/v3/team` 里的分配关系。
- main 上的 CI（de4df07，run 35815277270）：frontend 和 browser-smoke 成功，backend 失败。日志需要登录才能读，没有读到具体失败项；本地复现到的是上面两条路由契约测试。
