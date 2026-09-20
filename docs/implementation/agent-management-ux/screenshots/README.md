# 真实浏览器截图

图像文件就在本目录（上一轮只有文字索引，是疏漏，已补）。

来源：**可丢弃的本地实例**——临时目录 SQLite、端口 18931、真实服务与真实写入，
**不是预览夹具**，未连接正式服务器。截图用 Playwright 对该实例抓取，用完已关停。

| 文件 | 内容 |
|---|---|
| `01-catalog-desktop.png` | 目录页：每卡名称/用途/「开始对话」主按钮/「管理职能体」弱入口/「⋯」菜单；左侧无与「职能体」并列的「能力库」 |
| `02-management-default.png` | 管理页默认态：面包屑「职能体 › 会议纪要助手 › 管理职能体」；工作规范只给摘要 + 折叠「编辑工作规范」；「创建样例项目」在当前角色上下文 |
| `03-advanced-maintenance.png` | 「高级设置 / 维护」展开：模型配置、维护对话（生成/应用草稿）、版本回退 |
| `04-add-ability-team.png` | 添加能力 →「团队已有能力」：方法模块、沉淀能力、可用工具/准备中工具分列 |
| `05-catalog-mobile.png` | 375×812 窄屏目录页 |

`04` 拍摄时实例里没有已发布职能包，所以「可用工具」是空态。
**`06`–`08` 是补拍的真实操作链**：先在实例里造了一个**合成**已发布工具包
（`合成演示工具`，回显用途，不含任何客户资料），然后真机走完整条链路。

| 文件 | 内容 |
|---|---|
| `06-before-attach.png` | 挂靠前：「可用工具」列出「合成演示工具 v1」与「挂靠到本职能体」按钮 |
| `07-after-attach-list-updated.png` | 点击挂靠后**未刷新页面**，上方「已挂靠工具」立即出现「合成演示工具 v1 · 尚未检查」 |
| `08-pack-detail-with-context.png` | 带 `?agent_id=` 进入职能包详情：目标已预选「会议纪要助手」，并有「返回职能体管理」 |

同步核验（真实 HTTP / 真实 DOM，非截图推断）：
- 服务端 `GET /capability-packs/bindings/{agent}` 返回 `合成演示工具 v1`，`environment.status=unchecked` ——
  挂靠确实落库，且未被误报成「环境就绪」。
- 详情页 `select.value` = 该职能体 id，选中项文本「会议纪要助手」；页面含 `/agents/{id}` 返回链接。
- 管理页「已挂靠工具」区文本：`合成演示工具 v1 尚未检查 管理`。

浏览器内另行核实（非截图，JS 读真实 DOM）：
- 「⋯」菜单 Esc 关闭、焦点归还触发按钮、`aria-expanded=false`。
- 改名保存后区块标题与面包屑**同时**更新（未刷新）。
- `/ability-center?tab=packs`、`/modules?selected=abc` 旧链接可达，查询参数保留，选中态归「职能体」。
- `/agents/:id?mode=maintain` 深链接仍进管理页。

## 最终两项收尾（`09`–`10`，真实 `/agents` 路由）

| 文件 | 内容 |
|---|---|
| `09-catalog-import-entry.png` | 真实 `/agents`（渲染的是 `AgentCatalog`）右上「导入职能体」，点击后展开既有 `NativePackImport` 原表单 |
| `10-summary-refreshed-after-add.png` | 加入方法后「工作规范」摘要变为 `2 个 Skill · revision 2`，并显示成功反馈 |

真实 DOM 核对（非截图推断）：
- `/agents` 上 `section[aria-label=恢复职能包]` 存在，标题「恢复 webuddy 导出的职能包（v1/v2）」，含 `input[type=file]`。
- 加入方法前摘要 `1 个 Skill · 0 项验收断言 · revision 1`，加入后 `2 个 Skill · 0 项验收断言 · revision 2`；
  `performance.getEntriesByType('navigation').length === 1`，**页面没有重新加载**；提示语「已加入岗位清单，上方摘要已更新。」
