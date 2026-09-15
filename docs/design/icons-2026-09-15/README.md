# 图标与控件统一（2026-09-15）

本轮仅修改前端展示与组件测试，不修改功能、后端、API 或路由。

- `Icon` 是工作台唯一的 SVG 图标定义入口，沿用导航线性风格：24×24、currentColor、1.6 描边。装饰图标对读屏隐藏，按钮与链接保留文字或原有可访问名称。
- 退出、移动菜单、演练、外链、下载、返回、新建、列表箭头及原生 details 展开器使用统一图标。正文中的流程箭头与映射关系仍属于文本，不作图标替换。
- `CategoryBadge` 提供同一色块方徽；默认显示类别 SVG，可用单字或 emoji 覆盖。职能体保留现有名称首字符，模块使用类别 SVG。
- 模块“查看内容”和“编辑模块”均为次要文字操作，字体、字重、颜色与 hover 下划线一致。
- 遗留 skill 选项及已引用列表优先显示来源名称，否则显示正文首句摘要，附短标识区分；无正文时保留来源 ID 或完整模块 ID。引用 ID、版本与保存请求不变。
- 职能体页只有一个可折叠“导入”区块。两个标签分别用于恢复 webuddy 职能包和外部包适配人签；支持左右方向键、Home/End，切换时不销毁已有输入和上传状态。原有 `?project=` 链接默认展开外部包标签。

## 验证与截图

- `npm run build` 通过；`npm test`：29 个文件、209 项测试通过。
- `frontend/scripts/icons-browser-smoke.cjs` 使用完全隔离的展示数据，不访问后端、不执行上传。检查标签切换、展开器 SVG、类别 SVG、操作字体一致、亮暗主题与手机宽度无横向溢出。
- `verification.json` 为浏览器验证记录。截图是展示夹具，不代表生产项目数据。

| 页面 | 亮色 | 暗色 |
| --- | --- | --- |
| 原生职能包导入 | [亮色](agents-native-light.png) | [暗色](agents-native-dark.png) |
| 外部 skill 包导入 | [亮色](agents-external-light.png) | [暗色](agents-external-dark.png) |
| 能力模块 | [亮色](modules-light.png) | [暗色](modules-dark.png) |

复跑：启动前端开发服务（默认 127.0.0.1:5186），执行 `node frontend/scripts/icons-browser-smoke.cjs`。可通过 `UIUX_ORIGIN` 和 `BROWSER_EXECUTABLE` 指定本地地址及 Chrome 路径。
