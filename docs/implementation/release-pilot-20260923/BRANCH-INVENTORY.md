# 分支与工作树清单（2026-09-23）

基准：发布提交 `de4df07`（= 远端 main = 标签 `webuddy-maintenance-2026-09-23`）。"已包含"指 `git merge-base --is-ancestor <分支> de4df07`。
本轮**没有删除**任何分支或工作树；以下只是建议。

## 需要注意的（未包含或有未提交改动）

| 分支 / 工作树 | SHA | 情况 | 建议 |
|---|---|---|---|
| `main`（本地）@ `/Users/auntlee/workspace/自动化无人工厂实践` | 3dc6ac9 | 提交已包含于 de4df07，但工作树有 39 条未提交改动 | 不动。由用户决定这些改动去留；本地 main 落后远端 main，勿在其上 reset |
| `factory/T-gitignore-web-1` @ `.factory-worktrees/T-gitignore-web-1` | 152fa1c | 未包含，+1 提交（`T-gitignore-web-1 attempt#1`，工厂 worker 产出） | 用户确认不需要后可删 |
| `worktree-fix-git-version-tests` @ `自动化无人工厂实践/.claude/worktrees/fix-git-version-tests` | 068cb27 | 未包含，+1 提交（第七道闸门兼容 git<2.40） | 可能有价值，建议评审是否合入，勿直接删 |
| `codex/review-agent-management-ux` @ `.factory-worktrees/review-agent-management-ux` | 150ae0c | 已包含，但工作树有 1 条未提交改动 | 先看改动再处理 |

## 已包含、可在确认后清理（工作树均干净）

| 分支 | SHA | 远端 | 工作树 |
|---|---|---|---|
| claude/agent-management-ux | f0599bc | 同 | agent-mgmt-ux（服务器 `f0599bc/.venv` 仍被线上复用，**与本地分支无关，但勿混淆**） |
| claude/artifact-readback | 50c4e20 | 同 | artifact-readback |
| claude/meeting-tool-chat | 7125b1a | 同 | meeting-tool-chat |
| codex/budget-ux-20260920 | ed05a96 | 同 | budget-ux-20260920 |
| codex/enterprise-plugins | 363da96（上一线上版本） | 同 | 无 |
| codex/meeting-buddy-acceptance | 78b2d1d | 同 | meeting-buddy-acceptance |
| codex/operations-20260921 | b854a41 | 同（PR #6 仍有 merge ref） | 无 |
| codex/release-chat-tools | a9d605b | 同 | release-chat-tools |
| codex/review-artifact-readback | 40dcf16 | 仅本地 | review-artifact-readback |
| v3-conversation-workspace | dbc38c5 | 仅本地 | v3-skills-icons |
| v3-skills-icons | 0890542 | 同 | 无 |
| webuddy-next-n1…n9, n11, r1…r3 | 各异 | 仅本地 | 同名工作树 |
| webuddy-r1-t03…t06, webuddy-r2-t07…t10, t13, t14 | 各异 | 仅本地 | 同名工作树 |
| (detached) review-budget-ux-20260920 | 08c5857 | — | review-budget-ux-20260920 |
| 远端 codex/autonomous-factory-v3 | 150ae0c | 仅远端 | — |

保留：`codex/maintenance-subsystem`（已上线候选，operations-20260921 工作树）、`codex/release-pilot-20260923`（本轮文档分支）。

## 复现命令
```
git worktree list --porcelain
git for-each-ref refs/heads
git merge-base --is-ancestor <branch> de4df07
git -C <worktree> status --porcelain | wc -l
```
