# 会话产物回读：独立验收与发布

- 代码：50c4e20d5a9b934e20b8230f40c8c42fa7b9dfdc，来自 Claude 的 claude/artifact-readback。
- GitHub 集成：codex/autonomous-factory-v3，快进，无强推。
- 标签：webuddy-v1-artifact-readback-2026-09-20。
- 线上：https://harness.cloudwaveai.cn ，2026-09-20 03:20（北京时间）切换。
- 旧生产：67e9179，保留目录、依赖、数据库与配置备份。

## 本轮分工和范围

Claude 编码；Codex 审查、独立复现、服务器真实模型验收及部署。只增加当前会话成果的清单与只读回读工具，不改前端，不新增任务框架，不扩大管理员权限。

第一候选 dfc4c55 的已有23条用例通过，但 Codex 的中文/emoji长文件复现失败：64KB截断半个UTF-8字符，合法文本误报二进制。反馈给 Claude 后修复为完整字符分页，返回 next_offset；不可容纳完整字符的窗口或半字符 offset 明确报 bad_range。独立原始复现断言未改。

## 实际证据

- macOS：test_chat_attached_tool.py + Codex 原样外部 UTF-8 复现，29 passed。
- Linux：同候选、锁定依赖、同一29项，29 passed（46.59s）。覆盖会话与发起者隔离、真 worker、取消、分页和二进制拒绝。
- 无前端差异，复用已验证的前端构建；没有跑无关全量或重复前端套件。
- 真实 Claude（claude-sonnet-5）使用正式 SDKRunner → sdk_worker → MCP 权限闸门读取真实保存的会议工具产物。不是替换 runner 或 fake query。
- 实际工具调用：session_artifacts 一次，read_session_artifact 三次。先 max_bytes=64 读63字节，再 offset=63 读62字节，最后读全文。
- 独立提取 SDK 的四条真实 tool_result（不提取 thinking），逐条确认成功。三次回读均与 SQLite 保存的原始字节对应切片完全一致；分页连续；全文 sha256 一致。模型正确回答文件标题、乙方负责人（姓名待确认）、未记录双方明确共识。
- 此次真实模型验收费用回执：0.530668 USD。

证据边界：真实模型测试是绑定已有隔离验收会话的正式 SDKRunner 调用，记录真实工具请求及结果；不是声称本轮重新跑了整套浏览器上传/开发/部署流程。上一轮已经做过会议上传、工具执行、下载、人工修订、旧版保留，本次复用那些真实产物验证新回读链路。

## 发布与恢复

- 新服务目录：/home/ubuntu/releases/factory/50c4e20。
- factory-web.service 单进程，端口8788，域名和 Caddy 不变。
- 部署前确认无活跃运行/维护/工具任务；停服务后 SQLite backup API 备份并检查完整性。
- 备份：/home/ubuntu/releases/backups/webuddy-20260919T192050Z-pre-50c4e20。
- 该目录的 rollback-code.sh 可恢复67e9179代码与服务路径；不会自动覆盖上线后的业务数据。
- 服务 active；真实进程路径已核对；公网首页与已验前端构建哈希一致；未登录API401。
- 正式账号登录及7个关键接口200；10项目/15运行/11职能体/5会话的原ID全部保留；无自动重启。

## 仍须如实保留的边界

64KB是返回窗口上限，底层仍整份读取受既有总上限约束的产物（包产物16MB），没有把公共存储改成分块I/O。只读UTF-8文本，HTML返回源码，不执行。工具可回读不代表模型的业务推理一定正确，会议仍需人工确认；不能把格式/引文存在校验当作客户验收。

会议规范和样例工具仍属于隔离验收数据，本次没有迁移这些对象或覆盖正式数据库。本轮完成后暂停监工，不自行继续扩展功能。
