# 澄清问答交互的浏览器演练记录

**证据边界先说清楚：** 这一轮**没有调用任何付费模型**。规划器是
`scripts/preview_scenarios.py` 里带标注的脚本（第一轮只提问、第二轮才给计划），
provider 显示为 `codex/rehearsal-*` 只是演练配置的名字，**不是真实模型调用**。
真实的是：FastAPI 服务、页面调用的 HTTP 契约、控制库、插件闸门、项目自己的
pytest 检查、git 提交。素材全是本仓库自带的合成样例。

服务：`python3 scripts/preview_scenarios.py --port 8797 --data-dir /tmp/factory-clar`
（脚本规划器，未加 `--real-model`）。登录 `preview`。

## 三个页面的任务标识

| 场景 | 页面 | 任务/切片 ID |
|---|---|---|
| 信创化改造 | `/modernization/…` | `11b05a3fa16a4bc7bff339d7a4c52c4f` |
| 接口适配 | `/adaptation/…` | `c2df31b7449c41139d45072d1fdb7104` |
| 自动化运维 | `/maintenance/…` | `30d655b1b91a4dac8bbebfe9867a0cfe` |

三个页面都在浏览器里确认过：**同一个** `ClarificationPanel` 组件渲染出真实的
`pending_questions`、回答输入框和「提交回答」按钮，阻塞原因显示
`clarification.requested`。

## 「看到问题 → 回答 → 重新规划 → 批准」完整走通（信创页）

事件序列取自控制库，**回答与批准都是在浏览器页面上点的**，不是脚本直接调 HTTP：

```
user.message        按已授权的信创化改造流程处理下面这条范围明确的迁移切片。 仓库：preview/legacy-quote 必须基于基线 commit：c960609e2be70…
plan.created        revision=1
triage.decided      decision=needs_clarification  questions=3
user.message        1) 用 jdbc:dm:// 这种写法。2) mysql 现有输出必须逐字节不变。3) 只改 db_url.py 一个文件，新增 dm 方言映射即可。…
plan.created        revision=2
triage.decided      decision=human_approval  questions=0
human.approved      actor=preview  revision=2
check.result        name=regression  exit=1
attempt.failed      （项目检查判失败，进入修复轮）
check.result        name=regression  exit=0
git.commit          commit=f93d45ed143e
check.result        name=regression  exit=0
run.verified        commit=f93d45ed143e
```

- `user.message` 里那句 `1) 用 jdbc:dm:// …` 就是在页面输入框里敲进去、
  点「提交回答」送出的原文。
- `human.approved actor=preview` 是在页面上点「批准计划」产生的。
- 批准后项目自己的 `regression` 检查**真的失败了一次**（exit=1），
  修复轮之后 exit=0，再提交、验证、交付。

## 「回答」没有被「继续执行」顶替

停在提问上时，三个页面头部都**不再**出现「继续执行」：

- 信创：`nextAction` 对 `clarification.requested` 返回 `clarify`，头部只剩
  刷新 / 取消切片（之前会给「继续执行」）。
- 维护：`canResume` 在有 `pending_questions` 时返回 false。
- 适配：本来就没有 resume 入口。

resume 只会把停在提问上的运行推回同一个闸门，摆出来就是误导。

## 维护页当时的渲染文本（截自浏览器）

```
模型提出了需要确认的问题
回答之后会重新规划；这一步不是「继续执行」——停在提问上的运行需要的是答案。
  达梦方言的连接串按 jdbc:dm:// 还是厂商文档里的另一种写法？
  mysql 现有输出必须逐字节不变，还是允许格式化差异？
  Please provide at least one executable task.
你的回答 / 提交回答
当前状态 等待中
需要补充信息才能继续
模型在动手前提出了需要确认的问题，回答后才会继续
原始代码 clarification.requested
```

（第三条问题是平台在计划没有可执行任务时自己补的，一并如实展示。）

## 关于截图

浏览器截图在会话里逐步核对过（登录页、三个详情页的问答面板、回答提交后的
「批准计划」入口、批准后的状态）。**截图是以图片形式返回到会话的，工具不会把它
写到磁盘**，所以这里留的是可检索的页面渲染文本与控制库事件序列；
两者能相互印证同一次演练。控制库 `/tmp/factory-clar` 是临时目录，
本文件写完后即删除。
