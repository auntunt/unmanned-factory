# 职能体管理 UX 上线验收 — 2026-09-20

正式站：https://harness.cloudwaveai.cn/agents
上线代码：f0599bc0bda340f388df2f43577854dfae82508d。GitHub集成分支codex/autonomous-factory-v3已从40dcf16快进；本报告后续为纯文档提交，不改变运行代码。
里程碑标签：webuddy-v1-agent-management-2026-09-20。

## 用户可以做什么

- 在目录卡片菜单或管理页直接修改职能体名称、展示用途，不调用模型，不改变角色ID、工作规范、历史对话、工具绑定。
- 通过职能体管理页集中查看工作规范、已挂靠工具，选择上传包、团队已有能力或开发成果。能力库不再与职能体并列导航，底层共享资产和旧链接保留。
- 在当前角色直接加入已有方法、挂靠已发布工具；详情页保留角色上下文和返回入口；列表与规范摘要按真实结果更新。
- 原模型配置、维护对话、草稿应用、历史版本恢复保留在折叠维护区域；平台导出包的导入接到实际目录。

## Codex独立证据

- 早期候选85641b6的5条复现失败、8428b04的4条闭环复现失败均先留证再交Claude修，最终9条全绿，原断言逐字节核对。
- c0a221c相关前端34 passed、元信息后台12 passed、生产构建通过；真实UI发现实际目录导入遗漏与摘要不刷新，再次收口，不以总测试数代替用户路径。
- 最终f0599bc本机受影响前端35 passed（6文件），生产构建通过。未重复无关后台全量。构建有原有大chunk提示，退出0。
- 最终服务器Linux隔离测试库：元信息与manifest相关22 passed。使用独立候选目录和venv，未用正式库跑测试。
- 本机真实服务+真实SQLite+生产构建：目录导入按钮打开原NativePackImport；添加方法后摘要从1项/revision1即时变2项/revision2，不刷新页面。两张独立截图保存在本地验收目录。
- 旧本地实例在上一轮结束后退出，初次复开连接失败；重启独立实例并新建正常标签后完成真实验收，不将连接问题误记成产品问题。

## 部署与回滚

生产从50c4e20切换为/home/ubuntu/releases/factory/f0599bc。切换前确认无活动run、maintenance job、pack task；备份control.db、users.db、服务配置和运行配置。仅新增元信息审计表及触发器，无破坏性迁移。

备份：/home/ubuntu/releases/backups/webuddy-20260920T050938Z-pre-f0599bc
回滚代码脚本：同目录rollback-code.sh（回到50c4e20）；数据库备份保留，但不应随意覆盖上线后新增数据。
既有webuddy-v1-artifact-readback-2026-09-20标签未动。

上线后真实核验：
- 公网HTTPS /agents 返回200，HTML字节与本次构建完全一致；未登录API返回401。
- systemd active、NRestarts=0、WorkingDirectory指向f0599bc。
- agents 12、agent_versions 12、agent_conversations 6、projects 10、runs 15、pack_bindings 1、pack_versions 1：每张表的全部行摘要与切换前一致，不仅比较计数。
- 正式广联达会议纪要角色f2756d877ee84ce5aedba4a4c66c377a、1项Skill、绑定版本1575e8cbda64411c8155880db30ca0f9保留。
- 原对话和9个产物下载仍可读，最终minutes.md校验和79ff396af9661140517e25e1d6808d2a6403a2ba369ce32ef331a45680be80af一致。
- 新元信息路由对不存在对象返回404；没有为了生产冒烟新增/改名正式角色。实际写入验证在隔离环境完成。

## 证据边界

本轮没有新增真实模型执行验收（修改是普通元信息与前端工作流）；不据此扩大MFD支持或其他能力范围。正式站原会议成果已验证可下载。正式站浏览器工具连续连接超时，未取得上线后的新截图；正式站HTTP/登录读取/内容哈希检查通过，视觉截图来自最终候选的本机真实服务。旧全量测试不冒充本轮重新执行。

代码由Claude完成，Codex只做复核、复现、运维与记录。没有读取/发送凭据给Claude，客户原文未加入GitHub。
