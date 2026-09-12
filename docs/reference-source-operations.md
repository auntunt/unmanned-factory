# 垂直资料挂载：管理员接入说明

本功能不要求更换模型计费配置。CLI/JSON 导入使用平台基础依赖；MCP stdio 导入需要已安装的 MCP Python SDK，Claude 实际工具挂载需要现有 claude extra。仓库锁定环境中的 MCP 为 2.2.0。

## 1. 准备资料

统一输出结构如下。示例仅用于连通验证，不是工业标准：

```json
{"documents":[{"id":"units","title":"样例单位约定","uri":"knowledge://demo/units","text":"演示约定：长度字段用 mm。实际业务请替换为经核对的客户规范。"}]}
```

请先核对来源与内容；不要把密钥、登录信息或未授权客户资料作为文档导入。导入产生新集合版本，模块选择或槽位绑定决定它是否用于任务。不会自动把整个 MCP 工具集开放给模型。

## 2. 使用 CLI 接入

以下路径与 PROJECT_ID 为占位，需要换成实际控制数据库与项目 ID。shell 管道左侧可使用团队已有的资料导出 CLI，只要它输出上述 JSON。

```sh
factory-sources --db /path/to/control.db --project PROJECT_ID --actor operator \
  import-json --name 客户格式规范 --file /path/to/references.json

trusted-knowledge-exporter --format webuddy-json | \
  factory-sources --db /path/to/control.db --project PROJECT_ID --actor operator \
  import-json --name 客户格式规范 --file -
```

首次导入返回 id、revision、sha256、document_count，不打印文档正文。更新同一集合时加 `--source-id SOURCE_ID --expected-revision 1`，冲突时必须重新读取当前版本。重复导入会生成新版本；请由同步调用方管理是否需要更新。

## 3. 使用 MCP resources 接入

```sh
factory-sources --db /path/to/control.db --project PROJECT_ID --actor operator \
  import-mcp --name 客户格式规范 \
  --command /opt/team-tools/bin/knowledge-mcp \
  --uri knowledge://customer-a/format-spec
```

只能指定管理员已安装且可信的 stdio 服务；不要执行待维护项目里的脚本。附加命令参数使用重复 `--arg=VALUE`。需要凭据时，在当前管理进程配置环境变量，然后用 `--env-name VARIABLE_NAME` 指定名称；不要在 argv、URI、模块说明或示例 JSON 中填写凭据值。

只调用 resources/read，不自动调用 tools/call。默认总操作超时 30 秒，仅接受文本资源。外部服务需先自行部署；此 CLI 不负责自动安装远程程序。API/CLI 工具型知识库可先输出统一 JSON，后续再接实时网关。

## 4. 模块声明与项目绑定

推荐通过模块 API 创建可复用方法，body 例如：

```json
{"name":"工业格式转换","category":"knowledge","description":"依据项目规范转换格式","instructions":"先查询字段、单位和异常处理约定，再转换并验证样例。","source_slots":["format-spec"]}
```

然后为项目绑定具体版本：

```sh
factory-sources --db /path/to/control.db --project PROJECT_ID --actor operator \
  bind --slot format-spec --source-id SOURCE_ID --source-revision 1 --expected-revision 0
```

更换绑定时 expected-revision 指槽位绑定版本，不是数据源版本。绑定完成后在现有项目模块页选择该模块并保存。项目专用模块也可直接携带 source_refs，例如 `[{"id":"SOURCE_ID","revision":1}]`；项目内新建模块的编辑器可勾选已导入资料库。

同一方法模块可在其他项目使用，该项目需自行绑定同名槽位。跨项目直接引用集合会被拒绝。源版本或模块版本更新不会自动替换已经固定的任务版本。

## 5. 检查与停用

```sh
factory-sources --db /path/to/control.db --project PROJECT_ID --actor operator list
factory-sources --db /path/to/control.db --project PROJECT_ID --actor operator disable --source-id SOURCE_ID
```

登录后可读取：

- `GET /api/v4/projects/PROJECT_ID/sources`：本项目集合目录。
- `GET /api/v4/runs/RUN_ID/mounts`：任务的挂载版本、摘要与文档数量。

Claude 执行时调用 `mcp__references__search` / `mcp__references__read`。日志中的 mounts.dispatched 和 source.read 证明平台装配并收到查询；单纯创建数据源记录不证明模型已经使用它。

停用阻止后续模型派发；正在运行的调用仍持有已有快照。如需立即停止其使用资料，应取消该任务。当前版本没有实时远端查询、自动轮询同步、OAuth 托管或 Codex/DSH 资料工具适配。
