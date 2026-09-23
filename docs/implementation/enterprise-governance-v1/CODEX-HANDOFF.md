# Codex 交接：企业治理 v1 最小纵切

- 分支：`codex/enterprise-governance-v1`；候选 SHA 见下方推送后的 `git ls-remote`。本文件所在提交之前的代码 SHA 是 `ad5ef07`。
- 基线：`de4df07`（远端 main，与已上线版本一致）。不合并 main，不部署。
- 工作树：`/Users/auntlee/workspace/.factory-worktrees/enterprise-governance-v1`，状态干净。frontend/node_modules 是本地 `npm ci` 装的（不再软链到其他工作树），`dist` 不入库。

## 复核修正（REVIEW-3fd815f）
审计泄露已修，见 STATUS“复核修正”和 CONTRACT 中 `audit[]` 的规则。新增或收录的测试：`tests/test_codex_gov_review.py`（原样）、`test_project_moved_across_departments_hides_its_past_in_leader_audit`、`test_unit_moved_across_departments_hides_its_past_in_leader_audit`。
复跑：`uv run pytest tests/test_org_governance.py tests/test_codex_gov_review.py -q` → 12 passed。截图拍摄于修正之前；审计卡片上显示的字段（unit_path / target_username / project_name / result）在修正后仍然保留。

## 请独立验收
1. `uv run pytest tests/test_org_governance.py tests/test_codex_gov_review.py -q`（12 条）。
2. 通读 `factory/control/app.py` 里的 `member_read_scope`，以及 `governance.py` 的 `readable_projects` / `can_read_run`。核对 CONTRACT 里“旧入口读取收窄”的覆盖表和例外清单是否完整。
3. 截图与回执在 `evidence/`；可以用 `evidence/seed-synthetic-data.py` 加一个空的隔离数据目录重放（账号需要自己在隔离库里用 `AuthStore.create_user` 创建，脚本里没有密码）。

## 需要决定的事
- **成员读取收窄是行为变化**（见 STATUS），上线前要确认成员都已正确分配项目。
- 工作区级资产（能力资产、职能体等）要不要按部门隔离。
- `tests/test_app_route_contract.py` 在基线上已经失败，要由负责中间件审查的人更新哈希，还是保留现状。

## 子代理
页面由一个 Sonnet 子代理编写：按 `sonnet` 请求，自报 `claude-sonnet-5`（自报，未独立核实）。主会话（claude-opus-5-5）审查后修正了导航解析，并把它软链到其他工作树的 node_modules 换成了本地安装。后端契约、授权、测试和 git 操作都由主会话完成。
