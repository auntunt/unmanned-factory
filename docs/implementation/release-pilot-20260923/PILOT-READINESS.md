# 真实维护试点准备（2026-09-23）

## 结论：现在不能开始真实试点
在 webuddy 范围内已有的记录里，**没有**已授权的真实客户或业务代码库，也没有真实 Issue。已登记和验收过的都是合成仓库，不能拿它们宣布试点通过：
- `local/demo-invoice`（`synthetic: true`，临时 scratchpad 仓库）— `docs/maintenance-subsystem/REPLAY.md:15`
- 独立运行时验收用的新 clone 仓库（合成，单任务 $2.55）— `docs/maintenance-subsystem/INSTALL.md:151-154`
- 验收数据里那条"待批准"的机器需求也是合成数据 — `docs/maintenance-subsystem/CODEX-HANDOFF.md:77`

方案第20节把 P1 的前置依赖定为"仓库、构建环境、问题与验收人"，完成判据定为"代码、检查、回执和业务确认一致"（`方案与成果说明.md:540`）。这四项目前都缺。

## 需要用户提供（最小清单）
1. **仓库**：git URL 或执行主机上的目录（必须位于 `FACTORY_WORKSPACE_ROOT` 之下，`INSTALL.md:45`），加上项目名和分支。私有仓库需要平台上已配置好的凭据，只给**凭据名** `credential_ref`，不要粘贴密钥本身（`CONTRACT.md:60,77`）。
2. **固定基线**：开始试点时用的提交 SHA。
3. **真实 Issue**：原文或链接、复现步骤、实际与期望行为，以及它来自谁/哪个系统。
4. **构建与检查命令**：至少一条可在执行主机上跑的命令，登记后由探测建议并由管理员 `adopt`（`CONTRACT.md:64`）。
5. **允许修改范围**：可改的目录或文件、禁止触碰的部分（配置、迁移、密钥等）。
6. **业务验收人**：姓名或角色，以及对补丁做出确认的方式。
7. **数据与模型边界**：是否同意把该仓库代码发送给哪个执行器（Claude Code / Codex）和哪个模型、单任务预算上限，以及是否含个人或敏感数据。
8. **执行位置**：在本机还是在线上主机上跑执行器（线上主机要单独授权；本轮不访问生产）。

## 试点任务单模板
```
项目 / 仓库:            source=            name=            branch=
固定基线 SHA:
credential_ref（名称）:
Issue 来源与原文:
复现步骤:
期望行为:
构建 / 检查命令（adopt）:
允许修改范围:            禁止修改:
业务验收人 / 确认方式:
执行器与模型 / 预算上限:
数据边界（可否外发 / 脱敏）:
执行位置:                本机 / 线上（需单独授权）
产出保留:                补丁包路径、检查结果、事件日志、验收人反馈原文
结果判定:                代码 + 检查 + 回执 + 业务确认 四者一致才算通过
```

## 材料就绪后的一次试点流程（只跑一次）
`webuddy-maintenance repo add --source … --name …` → 等探测 → `repo adopt-checks <project_id> <检查>` → `submit --project <id> --text "<Issue>"` → `events <task_id> --follow` → 把补丁包与检查结果交给验收人 → 记录反馈原文，交 Codex 验收。

## 尚未验证（来自既有文档）
Codex 执行器、非 macOS 主机、生产域名下的同站子域嵌入 — `INSTALL.md:166`；以 URL 克隆的方式登记（真实网络路径）— `CODEX-HANDOFF.md:73`；跨站 iframe 里能否保持登录会话 — `INSTALL.md:164`。

---
盘点：只读子代理，按 `sonnet` 请求，子代理自报模型为 `claude-sonnet-5`（自报，未独立核实）。关键引用已由主会话抽查：方案 §20 第540行、INSTALL:45、CONTRACT:60-64。
