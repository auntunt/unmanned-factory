# 发布收口状态（2026-09-23）

## 已完成（P0）

| 项 | 结果 |
|---|---|
| 远端 main | `98780d8` → `de4df0720834f9d17dcab2cfb51f8d9dcfa1b613`，普通快进推送（`98780d8..de4df07`），未强推 |
| 发布标签 | `webuddy-maintenance-2026-09-23`，注释标签对象 `91a9e4e175b9914c3cbe4fc9a1f2c6c5b2dfa1b9` → 提交 `de4df07`；已推送。此前本地/远端均无同名标签 |
| 标签内容 | 上一版 `363da96`、线上地址、CLI、未接入的指标、发布证据目录 |
| 本轮工作树 | `/Users/auntlee/workspace/.factory-worktrees/release-pilot-20260923`，分支 `codex/release-pilot-20260923`（起点 de4df07） |

## 推送前核实
- 远端 `98780d8` 是 `de4df07` 的祖先（`merge-base --is-ancestor`）；增量 348 个提交、857 个文件。`363da96` 也是祖先。
- 仓库唯一工作流 `.github/workflows/ci.yml`：push main 只跑 backend/frontend/browser-smoke 测试，`permissions: contents: read`，**没有部署步骤**。候选中也没有 `git pull`/`origin/main` 形式的拉取式部署脚本（`git grep`）。
- 写入者：本工作树只有本会话；另一个 Codex 进程的 cwd 是 `Desktop/自动化harness构建`（统筹方），未发现第二个共享代码写入者。
- 推送使用现有 SSH remote；`gh` 未登录，没有登录，也没有改全局配置。没有遇到分支保护拒绝。
- 主工作树 `自动化无人工厂实践`（本地 main 3dc6ac9，39 条未提交改动）未被 checkout/reset/stash/clean。唯一副作用：共享仓库的远端跟踪引用 `origin/main` 因 fetch/push 更新为 de4df07（这只是跟踪引用，不影响它的工作树或本地 main）。

## 未验证 / 进行中
- main 推送触发的 CI：https://github.com/auntunt/unmanned-factory/actions/runs/35815277270，查看时状态为 `in_progress`。本分支此前 push 不触发 CI，所以这是 de4df07 的首次 CI。结果需要 Codex 复核；CI 失败也不影响线上（CI 不部署）。
- 分支保护的规则本身未读（需要 gh 认证）；只知道这次普通推送被接受了。
- 本轮没有跑 pytest/vitest，没有部署，没有访问服务器。

## 已知瑕疵（不改标签）
- 标签说明里写的 "CLI: bin/webuddy-maintenance" 在仓库里并不存在对应文件；仓库里它是 `pyproject.toml` 的 entry point，服务器路径那一段写对了。标签不覆盖，更正放在 `RELEASE-NOTE.md`。

## 文档
- `BRANCH-INVENTORY.md`：分支→SHA→是否已包含→工作树清单与清理建议（未删除任何东西）。
- `PILOT-READINESS.md`：试点准备清单与任务单模板。
- `RELEASE-NOTE.md`：简短发布说明（入口、CLI、未接入指标）。
- `CODEX-HANDOFF.md`：交接。
