# webuddy-next 复核修复交接（2026-09-19，基线 `4d2e3f8` → 候选见推送）

针对你在 `4d2e3f8` 上复现的三项 P1 阻塞。**只修这三项**，未扩知识库、未动 pause 状态机、未改 UI 设计语言。

## 一、三项修复

### R1（`26dd137`）会话 Skill 正文保存与实际装载
- 正文存新表 `session_skill_bodies`，ZIP 原样存入（`session_skills.py`）；GitHub 走导入时确定的 commit 内容，运行时不重拉活动分支（`github_skill_fetch.py`）。
- 装载复用既有受控 mount：`mounts.py:161-195` 在 `compile_mounts` 里新增 `session_skill_snapshot` 段，trust=`reference_data`，复用既有 `append_document` 与归属校验，**没有另造装载机制**。
- 两条链路都打通：无项目 do 聊天 `agent_routes.py:358-361`；coding 链路 `run_execution.py:38-43` freeze → `run_execution.py:77` 透传 `compile_mounts`。
- `capability_sources.loaded` 改为**只认实际装载证据**（`capability_source.py`），登记元数据不再算已装载。既有 `tests/test_capability_source.py` 相应补 `mount_snapshot` fixture——**只补装载证据，没有削弱任何断言**（diff 可核）。
- 另有测试覆盖：解绑后已启动调用仍持原快照；正文里的指令只作为 reference 数据存在，不进权限或系统提示。

### R2（`50fb54a`）会话 Skill 访问授权
- `session_skill_routes.py:24-31` 新增 `_verify_session_owner`：取会话 `actor_id` 与 `request.state.user['id']` 比对，admin 放行，不匹配 403。GET/POST/DELETE 三端点统一调用，**在取内容与外部拉取之前执行**。
- member 窄接线：`app.py:239-245` 白名单只放行 `re.fullmatch(r'/api/v4/sessions/[^/]+/skills(?:/[^/]+)?', path)`，归属由路由处理器验证。`/api/v4/agents`、`/api/v4/skill-ingestions` 等仍 admin-only。
- 覆盖：无关 member GET/导入/删除各 403；member 能导入自己的会话；`origin=github` 越权导入时**拉取层调用次数为 0**。

### R3（`0f224d9`）配置对话作业状态生命周期
- pending 在 `start_maintenance` **之前**落库（`agent_routes.py:721-732`）；完成时 `_update_admin_config_msg` **就地更新**那条 pending→completed（`:754`、`:765-777`），不再追加新消息。
- 失败与取消各有真实终态（`:780-784`，区分 `ProviderCancelled`）；服务重启后残留 pending 由 GET 端 `_resolve_stale_pending` **依作业状态恢复，不盲目标成功**（`:613-645`）。
- 沿用既有 maintenance job 机制，**未新增第二套作业框架**。
- 前端：`AdminConfigChat.tsx` 完成后停止轮询并调用 `onConfigChanged`；原有 4 条测试保留未删，新增第 5 条行为验证。

## 二、证据

**你的三条最小复现，在集成工作区 `v3-skills-icons` 逐条转绿**（复现文件与你的原件 `diff` 逐字节一致，三个 worktree 均未改动其断言）：
- 修复前：`3 failed, 2.03s`
- R1 合并后：`2 failed, 1 passed`
- R2 合并后：`1 failed, 2 passed`
- R3 合并后：**`3 passed, 1.81s`，退出码 0**

其余（集成工作区，`uv run --extra codex pytest -p no:randomly`）：
- **你的原定向集 5 文件：53 passed**（你当时 49，差值为本轮新增回归）。
- 相关定向 `-k "session_skill or mount or capability_source or run_execution or admin_config or agent_chat or auth or permission or role or conversation"`：**305 passed / 1 skipped**。
- 后端全量：见本文件末尾「全量结果」一节。
- 前端：`tsc --noEmit` 0、`npm run build`（`tsc -b` + vite）0、`vitest` **388 passed / 50 files**。

**主会话补跑的变异验证**（R2 回执里写的是「源码审计 + 行为验证」，我认为那比真跑变异弱，所以自己跑了一次）：把 `app.py` 的窄白名单改成 `path.startswith('/api/v4')` → `test_member_still_blocked_on_other_v4_write` 与 `test_mutation_wide_whitelist_breaks_test` 双双变红；还原后 7 passed。窄授权确实是窄的。

## 三、对我方上一轮验收的检讨

你这三条里有两条，是我方 V1「真实服务器核查 6/6 通过」**没能覆盖的测试设计盲区**，不是执行者谎报：
- 第 2 条越权读取：V1 验的是「跨会话 URL 带错误 skill_id 删除返回 404」，**没有验「无关用户拿正确 sid 去 GET」**。
- 第 1 条未装载：V1 验了绑定存在、进程重启后仍在、前端能显示，**没有验「正文真的到了 runner 手里」**。

教训已入库：**绑定表有行 ≠ 能力被用上**。凡"导入/加载/挂载/绑定"类功能，验收必须有一条唯一标记穿透到最终消费者的断言；凡带资源 id 的读接口，必须有一条无关用户拿该 id 去读的断言。你那条"正文埋唯一标记、断言它出现在最终 ProviderRequest"的复现写法，我方已采纳为此类功能的标准验收手法。

## 四、环境文档更正（已采纳你的现场检查）

`codex-handoff.md` 原称 `FACTORY_GITHUB_TOKEN` / `FACTORY_DEPLOY_KEY_DIR` 未配置——**该说法错误，已更正并标注**：那是本机环境观察，被错误地套用到了生产。生产上两者均已配置（你只输出布尔值，未读取值）。`deploy_targets` 仍 0、固定公司测试地址仍缺，这两条不变。

## 五、仍待你验收（我方未做，也不自行去做）

1. 配置对话的**真实模型**链路。
2. **私有仓库** + token 的 Skill 内容导入。
3. 旧开发现场与发布链路复跑。
4. Linux 下本轮三项修复的复验（我方为 macOS）。

我方未访问服务器、未读取任何凭据、未部署。`paused` 独立状态继续后置。

## 六、全量结果

`uv run --extra codex pytest tests -m "not smoke" -q -p no:randomly` → **2599 passed / 0 failed / 20 skipped / 2 deselected**，退出码 0，733.90s。

你在复核意见里说「不重跑无关全量」，我还是跑了一次——因为本轮改动落在 `app.py` 中间件（权限边界）、`mounts.py` 与 `run_execution.py`（每次运行都经过）这三处全局路径上，不是局部改动。上一轮正是全量才抓出配置工具暴露面的缺陷。三次全量对照：`16dfdaa` 2582 → 本轮 2599，增量全部为新增回归，无既有用例退化。
