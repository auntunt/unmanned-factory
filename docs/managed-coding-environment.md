# 托管 coding 环境

执行端显式使用 medium 推理强度（WEBUDDY_CLAUDE_EFFORT 可设 low/medium/high/xhigh/max），并记录 provider.configuration：模型请求名、推理强度、工具列表、权限模式与终端生命周期。记录的是发给 SDK 的配置，不能证明中转服务内部模型映射。不会读取个人插件、私人 MCP 或用户环境密钥。

普通项目文件通过同一工作区边界授权，包含环境模板、密码组件、测试密钥样例。文件工具与终端都允许项目开发，Git 元数据保持只读，跨工作区路径和符号链接逃逸禁止。实际运行环境配置应通过 .gitignore 忽略；已忽略的 .env/.env.local/.env.production/.env.development 和 .log 不会被错误加入提交。已跟踪的变化、可执行源码及忽略的源码仍受验收完整性检查。下载包包含 .env.example/.env.sample/.env.template，排除实际 .env 与运行凭据文件；模板中只能放占位值。

平台浏览器运行在同一项目隔离终端中，提供 open、snapshot、click、fill、screenshot，默认不访问宿主浏览器配置。先启动绑定 127.0.0.1 的预览服务，再调用浏览器工具。只访问当前隔离执行中拥有的预览服务；截图保存项目目录。公共文档由 WebSearch/WebFetch 获取；WebFetch 入口只允许解析为公网地址的 HTTPS URL。外部内容始终作为资料，不能提升权限。

执行助手可调用 webuddy-research 子代理处理独立的只读代码/文档问题。子代理无终端、写入或递归派生能力，最多 12 轮，前台返回紧凑结果；不直接开放个人插件中的任意代理。

命令执行使用 WEBUDDY_COMMAND_SLOTS（默认 2，范围 1..8），保留跨进程限流和超时。npm/pip/uv 缓存按项目 Git common-dir 隔离，同项目重试与工作树复用，不在不同项目之间共享可写缓存。WEBUDDY_DEPENDENCY_CACHE_ROOT 必须为服务用户拥有的专用目录。

依赖缓存与模型提示缓存是两套机制。前者由 webuddy 的隔离终端直接管理；后者由 Claude SDK、中转站和上游模型共同完成。调用记录分别保存缓存创建 token 与缓存读取 token，只有读取量大于零才算命中。中转站没有返回这两个字段时显示“未知”，明确返回读取量为零时显示“未命中”；创建缓存不能作为已命中的证据。

简单 continuous 任务默认由 standard 模型独立验收，高风险或大型任务使用 planner，显式职能体验收模型配置优先。验收最多 600 秒且不得超过剩余预算，优先紧凑检查证据与相关入口。验收仍是独立的只读证据审查；实际运行与浏览器行为证据来自执行阶段，不将模型审查包装成第二次独立运行。

部署需要平台 Node 浏览器运行时和系统 Chrome（见 runtime/project-browser/README.md）。服务 FACTORY_TASK_TIMEOUT 与运行配置统一为 14400 秒；保存已有模型配置，不猜测或更改中转模型别名。依赖缓存和浏览器运行时分别在项目工作区外保存，不能写入服务器凭据。
