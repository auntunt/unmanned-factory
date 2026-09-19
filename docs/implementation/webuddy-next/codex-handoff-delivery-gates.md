# 交付收尾两处断点修复（2026-09-19，基线 `c5e6bff` / 代码 `ebf0a64` → 候选见推送）

只修你在 Docker 交付现场发现的两个断点。未访问服务器、未读密钥、未发布、未调用线上模型，RC1 与生产 tag 未移动。

## 断点一：平台截图被记为未声明修改

### 复现
`tests/test_scope_platform_browser_artifacts.py`。按现场同形构造：平台写 `.webuddy/browser/preview-<uuid>.png` 并发 `browser.observed` 事件，随后对账。修复前 3 红。

### 修复：受控来源 + 可验证内容，两个条件都要满足
`scope_declaration.platform_browser_artifacts(store, rid, workspace, commit)`：

1. **路径必须是本次运行的平台登记**。路径由平台 bridge 用随机 UUID 选定（`runtime/project-browser/bridge.mjs::screenshotPath`），模型无从指定；每张在 `browser.observed` 上逐一登记。
2. **提交进去的字节必须仍是平台产出的那份**。这条是采纳你的提醒加的——事件只证明平台**曾经写过**该路径，不证明后来提交的内容没被换掉。

`project_browser.py` 在产出截图的那一刻记录 `screenshot_sha256` 与 `screenshot_blob`（git blob id）。对账时用 `git rev-parse <commit>:<path>` 取提交里那份的 blob id 比对——正是「提交的字节」，且**不需要解码二进制**。

**没有做**：不豁免整个 `.webuddy/`、不豁免所有 PNG、不按文件名模式豁免。

### 失败保守 + 旧现场如何恢复
旧事件（含现场的 675 / 985）**没有内容绑定字段**，按新规则**不豁免**，对账仍会判未声明。这是刻意的：**我没有、也不会给旧事件补造过去的内容哈希**——那等于凭空捏造证据。

旧现场的恢复只有这几条诚实路径，请你选：
- 在新候选上**重跑**该运行，新截图自带绑定，对账自然通过；或
- 由执行端把这两张截图**纳入 scope_declaration 声明**（它们确实进了提交）；或
- 保留 `scope:reconciliation` 这一条 fail，人工复核后在验收记录里注明原因。

### 回归
- 平台登记且内容一致 → 豁免。
- **同路径内容被改写 → 仍是未声明**（你点名要的那条）。
- 同目录、同 `preview-*.png` 命名但平台没登记过 → 仍是未声明。
- 别的 run 的登记 → 不豁免本 run。
- 旧事件无绑定 → 不豁免。
- 有截图在场时，普通业务源码改动照样被抓。

变异：去掉内容比对只认路径 → 「同路径被改写」立刻变红。

## 断点二：验收回执引用历史失败编号，直接 needs_human

### 复现
`tests/test_verification_browser_receipt.py`。现场同形：模型把 `recent_failures` 的历史失败编号（favicon 404，已修）一并写进 `browser_review.event_ids`，引用与快照 `latest` 不等 → 判失败且无纠正机会。修复前 2 红。

### 两个语义，明确分开
- **调用前快照**（`browser_observations`，渲染进提示词）：模型**只能**引用它——本轮它自己的新观察，事件编号在提示词构建之后才产生。所以**引用闸门仍以快照为准**。
- **本轮结束时的最新观察**（`latest_browser`）：只用来判定「是否还有未解决的失败」。某个 task 的旧失败被更新的观察取代后就离开 `latest`（进 `recent_failures`），因此**已修好的 404 自然不再阻断**；真正未解决的仍留在 `latest` 并继续阻断。

**中途我走错过一次**：先把引用闸门也改成对照「调用后」的观察，结果 `test_fresh_verifier_browser_observation_resolves_old_environment_failure` 变红——那要求模型引用提示词之后才存在的编号，**根本无法满足**。已改回以快照为准，这条既有测试恢复绿。

### 有界纠正
仅当缺口是**纯引用/结论表述**（`_RECEIPT_ONLY_GAPS` 两条）**且本轮新观察没有未解决失败**时，复用同一个验收会话给**最多一次**纠正：
- 复用既有 `coverage_retry` 的同款机制，不新增作业框架；
- 受同一 `review_deadline` 剩余时间与预算约束，取消即停；
- 反馈只说明「引用错在哪、该引用快照 `latest` 的哪几个、不要带 `recent_failures`」，**要求模型自己重述结论，不把 ids 写进 verdict**；
- **绝不把已通过的业务代码退回 coding 重写**。
- 仍不对 → `verdict=fail` + `error_type='browser_review_unreconciled'`，即「回执未对账」，**不伪装成功能失败，更不放行**。

### 你代码审阅点名的交错路径（已复现并修）
你说 `coverage_retry` 递归未透传新标志、可能重置浏览器纠正计数——**属实，而且我写测试后确实复现出 2 次纠正**。已让 coverage 递归透传 `browser_receipt_retry`，整个独立验收全过程**最多一次**浏览器回执纠正，deadline 与预算不变。
（你说这是待验证点、不是实测复现；我这里是本地测试复现，不是服务器现场。）

### 回归
引用错 → 一次纠正后通过；纠正后仍错 → 不通过且标 `browser_review_unreconciled`；真实未解决错误 → 不纠正、不通过；**引用错 + 同时有真实失败 → 不得因「先报引用问题」换来纠正机会**；coverage 补齐不得重置纠正次数。

变异四条各自如期变红：取消有界纠正、真实失败不再优先、把回执问题标成功能失败、去掉内容绑定。其中「真实失败不再优先」第一次变异**没有变红**，说明我的测试有盲区，补了「引用错 + 真实失败」组合后才真正承重——记录在此。

## 测试结果

集成工作区，`uv run --extra codex pytest -p no:randomly`：
- 两组复现：修复前分别 3 红 / 2 红 → 修复后全绿。
- 定向 `-k "verification or scope or spec_tree or browser or operation_workflows or acceptance or evidence"`：**208 passed / 2 skipped**。
- 按你的限定：**未跑后端全量、未跑前端、未调用线上模型**。

## 边界

1. **旧现场不会自动恢复**，见上面三条路径，需你选择。
2. 若 git 仓库用 sha256 对象格式，`rev-parse` 返回 64 位 id 与我们记录的 sha1 blob id 不等 → **失败保守，不豁免**（不会误放行）。
3. 内容绑定在**产出时**读取文件计算；文件当时不可读则不记录绑定，该截图不豁免。
4. 本轮所有证据均为本地测试，**不是服务器现场**。真实模型下的回执纠正、Docker/IP 全流程仍归你。
