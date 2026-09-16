# 图标来源与许可

工作台图标改用 **Phosphor Icons**（https://phosphoricons.com），MIT 许可，允许商用且无需署名。
图形从 `@phosphor-icons/core@2.1.1` 下载，作为内联 SVG 正文原样嵌入 `frontend/src/workbench/Icon.tsx`，
运行时不额外拉取网络资源，构建可复现。

- 导航与操作图标：`regular` 线重（viewBox `0 0 256 256`，`fill=currentColor`）。
- 职能体身份徽标：`duotone` 两色重，一职能体一枚，按内置职能包 slug 匹配语义：
  - 老项目渐进改造 → `recycle`
  - 项目代码梳理 → `tree-structure`
  - 自动化 CLI 构建 → `terminal-window`
  - 能力自进化设计 → `dna`
  - 需求分析 → `list-checks`
  - 逆向工程 → `magnifying-glass`

每枚徽标带一个强调色（`--wb-badge-accent`），背景与字色由该色经 `color-mix` 派生，自动适配亮/暗主题。
自定义职能体没有内置 slug，仍回退到名称首字符头像。

许可证全文见 https://github.com/phosphor-icons/core/blob/main/LICENSE 。
