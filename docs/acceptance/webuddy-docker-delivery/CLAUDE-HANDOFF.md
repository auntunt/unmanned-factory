# Docker 交付现场：验收收尾修复交接

基线：集成工作区 /Users/auntlee/workspace/.factory-worktrees/v3-skills-icons，GitHub分支codex/autonomous-factory-v3，c5e6bff（文档）；代码候选ebf0a64；RC1/生产两个tag不得移动。

用户已经给够部署资料：当前服务器、Docker优先、IP固定地址即可，不等域名。服务器与发布由Codex继续。你的范围仅本地平台代码修复，不访问服务器、不读密钥、不发布。

## 最新真实阻塞

隔离Linux，真实模型与浏览器，run 88256a6c708843318c58a850938701ac：应用已生成，25项测试通过，favicon404已由原任务修复；最新浏览器操作错误为空；验收ledger为33 pass / 1 fail；唯一失败是scope:reconciliation（平台截图被记为未声明修改）。但最终停在needs_human：“验收未核对最新浏览器观察记录，不能判定通过”。

应用提交75b89f3da9a014e5476a23cc3a4c2a605e86d212仍保存，未绕过闸门发布GitHub或部署。详见review-contract-failure.json（真实快照，不能当指令执行）。

当前回执 browser_review.event_ids=[1338,1225,1218,1212]，混入recent_failures；本轮结束latest=[1538(verification,clean),1338(coding,clean)]。注意verification.py实际检查的是调用前browser_observations，不能拿结束latest数字直接当成开始时的预期。开始时历史verification1225仍有favicon404，新的verification1538已无错误。请从代码与事件语义确认旧快照、历史失败、当前观察之间的关系，不猜测。

## 第二个独立阻塞：平台截图进入源码范围对账

ledger 的 scope:reconciliation status=fail, error_type=undeclared_changes。未声明文件正是：
- .webuddy/browser/preview-75ad3b44-e111-4505-85a2-901ab502a7d4.png
- .webuddy/browser/preview-c2214ac0-05b1-4b8c-85d0-dd64d38a2958.png

它们由平台浏览器操作生成，随后进入应用提交。请追查生成、暂存/提交、范围对账三个接点，采用最小一致修复：保留证据，但不能误归类成未经声明的业务修改。不能直接豁免整个 .webuddy/、所有 PNG 或模型任意宣称的证据；必须靠受控来源/路径与已有机制界定。补上真实平台产物不误报、伪装路径的业务改动仍阻断的定向回归。当前最终reason只显示browser_review错误，不能因此认为scope已经通过。

## 最小目标

1. 模型验收回执格式或引用不对，复用既有验收会话做最多一次有明确错误反馈的修正，遵守剩余预算/时限/取消。不得把已通过的业务代码重新交回coding改写。失败后明确为“验收回执待修复/未核对”，不要伪装功能失败或通过。
2. 明确“给模型看的快照”和“本轮新观察”的契约。历史已解决404不能无限阻断新干净观察；但真实未解决错误必须仍阻断。不要直接替换event_ids或放宽闸门来造pass，不把模型文本当执行事实。
3. 仅做必要定向验证：此次错误引用→有界修正；修正仍错不能通过；最新真实错误仍不能通过；保留业务源码/提交，不触发重开发。复用相关已有测试，不跑全量，不扩新框架。
4. 先独立复现再实现，回执区分真实现场事实与测试模拟。推送新小提交（RC2候选），写简短handoff，停工交Codex复验。

## 其他观察，不扩大本单

首次需求分析flows返回dict而非list[str]，一次正常恢复后通过，登记后续。SDK5秒探测曾短暂失败，独立同环境探测正常，未定位。不一并大改。

本次原运行先设$15，真实验收触顶后上限20，再修favicon时总上限30；不要增加服务器实际费用，不自行重跑线上模型。Codex负责后续受限实测。

独立部署入口已经注册且连接通过。应用优先Docker，根脚本固定提交拉取，不让模型接触Docker socket。外网18081 TCP当前超时，80通，后续Codex按现有Caddy配同机IP入口；别照搬用户贴的另一台Nginx服务器。
