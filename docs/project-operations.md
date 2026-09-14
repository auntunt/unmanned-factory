# 项目维护与运维闭环

## 入口与版本

六环看板保留。项目概览固定提供需求/维护入口，前端从只读 `GET /api/v2/operation-presets` 拉取名称、提示、字段和版本；不维护第二份菜单。`operation_presets(id, version, data)` 保存版本，启动时用 `templates/operations-v1.json` 做幂等初始化，已有版本不覆盖。后续版本以新行迁移，不修改旧运行。

提交仍使用 `POST /api/v2/runs`，`operation` 默认 general，新增可选 `operation_fields`：

| 类型 | 字段 |
| --- | --- |
| bugfix | error_log（错误日志）、reproduction（复现步骤） |
| startup | command（启动命令）、port（端口） |
| release | environment（目标环境） |
| dependencies | package_manager（包管理器）、version_constraints（版本约束） |

所有补充字段可不填；后端按数据库字段白名单验证，每个字段最多 8000 字符，去首尾空白并忽略空字段。字段编译进请求，也参与有序规范化指纹。无字段请求保持上一版的指纹算法。幂等键仍按用户+项目隔离；同键同内容返回原运行、不重派规划，同键内容变化返回 409。目录版本变更不破坏旧提交重试；运行保存实际 operation_version，编译后的说明冻结在请求中。

## 结果卡片与验收

维护结果卡片使用同一次验收的 operation_results，与 acceptance_ledger 的 criterion_ids 关联；不存在的证据引用不展示，未验证项不能变成通过。Bug 展示回归结论；启动排错展示最终启动命令和健康检查；部署准备展示构建产物、健康检查和回滚步骤。缺少证据时明确未验证。无新增验收模型调用或强制执行阶段。

验收说明位于 `templates/verification-v1.txt`，记录 verification_template_version。模型必须覆盖逐项标准；总评不替代逐项证据。

## 本机定时巡检（默认关闭）

项目页可开关、选择间隔、查看上次入队时间、状态、结论和证据。`GET/PUT /api/v2/projects/{id}/inspection` 使用配置 revision 防止覆盖；修改仅管理员可用。默认间隔一小时，允许 5 分钟至 7 天，启用后在一个间隔后首次入队。

调度器持有现有队列租约，每 5 秒检查到期项目，原子写入巡检运行、持久队列和下次时间。有活动任务的项目暂不重复入队；停机错过多个周期只补一次，不追赶所有历史周期。巡检与普通工作共享最多 4 个执行槽，模型派发前检查既有预算和项目授权，沿用费用记录与恢复机制。

**巡检不含远程服务器，也不提供服务器连接或凭据管理。** 只检查本机可达、能够获取 Git 已提交快照的项目工作区。未提交文件和本机密钥不带入副本，缺配置会报告未验证；巡检不证明原线上服务正在运行。

巡检直接使用隔离验收，不进入编码/修复/发布路径；可在一次性副本创建临时探针和构建输出，但不能修改既有源文件或原项目。通过记录 inspection_completed；失败或无法验证记录独立终态 inspection_failed，留下原因和证据；下一次巡检入队时将旧的未处理巡检标记为已被取代，attention 只显示最新一条巡检结论。原巡检不能通过继续/重试操作转为编码任务，修复需另行提交维护需求。中断的验收不会假定成功，也不会重放发布。

## 浏览器降级与容器测试

`FACTORY_BROWSER_STARTUP_TIMEOUT_S` 默认 30 秒，范围 1–120 秒。bridge 启动失败最多重启一次，清理本次启动进程后重试。环境缺失、Chrome 启动失败和浏览器超时记录 browser_unavailable，验收中显式增加 unverified 证据；其他标准继续留证。真实页面错误和实际功能失败仍为 fail。

未验证不会被算作通过，不自动发布，也不因浏览器基础设施故障触发代码修复。界面在看板、六环阶段和结果页单独显示未验证；恢复浏览器后可以再核对证据。新浏览器成功观察可消除较早的环境不可用记录。

保留 `runtime/project-browser/Dockerfile.smoke`，可运行 Linux + Chromium + bubblewrap 的真实隔离终端、HTTP 预览、浏览器读取与 PNG 截图测试。沙箱不可用直接失败，避免 smoke 被静默跳过。容器仅在隔离测试环境使用嵌套 namespace 所需权限，不挂生产卷、不传生产凭据。按本轮交付要求，只推送代码，不配置 GitHub Actions 自动运行。

旧 pages、controlroom 与 App 引用图之外的 components 已删除，连同对应测试；现役六环看板和其依赖保留。
