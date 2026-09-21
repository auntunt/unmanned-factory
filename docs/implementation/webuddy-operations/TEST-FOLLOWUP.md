# 后置测试清单（功能优先一轮）

本轮按 CLAUDE-FUNCTION-FIRST.md 的边界执行：只做直接修改路径的定向检查、一次前端生产
build、一次连真实后端的核心用户路径。下面是**知道但本轮没有验证**的事项，交后续独立测试
任务。每条给出位置、事实、影响和建议的验证方式。

这里记的是"广泛测试工程"，不是已知缺陷。已知缺陷在 STATUS.md 里写明，未修的会明说。

## 1. 维护任务 HTTP 面的并发与幂等

- 位置：`factory/control/maintenance_routes.py`
- 事实：`POST /tasks` 的幂等由 `MaintenanceTasks.create` 的 idempotency_key 保证，M3 已有
  单元覆盖；但**并发两个相同 key 的 HTTP 请求**没有在路由层验证过。
- 影响：并发重复提交理论上由 `claim()` 的 `BEGIN IMMEDIATE` 挡住，未经 HTTP 层实测。
- 建议：两个线程同时 POST 同一 key，断言只产生一个 task 和一次 dispatch。

## 2. 导出与产物下载的大文件行为

- 位置：`maintenance_routes.download_artifact`
- 事实：产物整体读进内存后由 `Response` 一次性返回。演示补丁 602 字节。
- 影响：大补丁（数十 MB）的内存占用和超时未测。
- 建议：构造大 patch，量内存峰值，决定是否要改成流式。

## 3. 维护任务列表的规模

- 位置：`frontend/src/workbench/MaintenanceTasksPage.tsx`
- 事实：列表一次取回该项目全部任务，前端排序，没有分页。
- 影响：单项目任务上千条时首屏与轮询开销未知。
- 建议：造 1000 条任务，量列表接口耗时与前端渲染时间。

## 4. 8 秒轮询在多标签页/长时间停留下的表现

- 位置：列表页与详情页的 `setInterval(..., 8000)`
- 事实：详情页在 `document.visibilityState !== 'visible'` 时跳过轮询；列表页没有这层判断。
- 影响：多个后台标签页会持续发请求。
- 建议：开多标签页观察请求量，必要时给列表页加同样的可见性判断。

## 5. 计划批准的权限边界

- 位置：`maintenance_routes.approve`
- 事实：批准前走 `tasks.get(...)` 做项目授权，与其他任务动作一致；但**member 角色是否应当
  可以批准计划**没有产品决定，也没有测试。
- 影响：目前只要能看到该项目的任务就能批准。
- 建议：先定产品规则，再补"无权者批准被拒"的断言。

## 6. 跨浏览器与移动端

- 事实：本轮只在 Chromium（Playwright / 应用内浏览器）桌面宽度下走过流程。
- 影响：Safari/Firefox、窄屏布局未验证。
- 建议：至少补一次 Safari 与 375px 宽度的核心路径。

## 7. 新方法包的业务效果

- 位置：`factory/control/builtin_packs/packs/issue-maintenance/`、`.../api-adaptation/`
- 事实：两个包 `validation_status` 都是 `template-unbenchmarked`，只验证了目录可加载、
  zip 可复现、模块指令进了 agent.json 与 SKILL.md。
- 影响：**没有任何业务效果证据**，不能对客户宣称已验证。
- 建议：等客户资料齐了，用真实 Issue 做留出集评测，再改 validation_status。

## 8. CI 上的这批新增

- 事实：本轮新增的后端与前端测试只在 macOS 本机跑过。
- 影响：Linux runner 上的表现（尤其是依赖 git 与子进程的维护路由测试）未知。
- 建议：观察下一次 CI，只读第一处新失败。
