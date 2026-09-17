# CSV 工程量清单 → XML

这是一个**真实的小型转换工具**，用于打通「开发产物 → 候选包 → 验证 → 发布 → 挂靠 → 调用 → 下载」
整条职能包链路。它不是 MFD 转换器，也不代表 MFD 能力已经具备。

- 程序：`tool/main.py`（调用入口）、`tool/quote_xml.py`（解析与字段映射）。
- 验收：`fixtures/tests.json` 两个用例——标准清单逐字节比对期望 XML；缺列必须返回 `bad_header`。
- 范围：`webuddy-pack.json` 的 `support_matrix` 写明已验证与未验证的格式，不声称通用。
- 样本：`fixtures/` 全部是合成数据（material_scope=synthetic），不含任何客户资料。
