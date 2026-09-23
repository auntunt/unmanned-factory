# 交接：私有 GitHub 仓库接入失败修复

- 分支 `codex/maintenance-repo-auth-fix`，基于 main `ac0944b`（已上线，里程碑 `webuddy-governance-2026-09-23`）。候选 SHA 以推送后的远端为准。未部署，没有访问服务器，没有读取任何凭据。
- 现场：`auntunt/group-risk-data-system` 用 HTTPS 登记后，克隆报 `could not read Username … terminal prompts disabled`。根因是 `_clone_and_probe` 没有接入服务已有的 `FACTORY_GITHUB_TOKEN`，`credential_ref` 只保存不生效；克隆失败后，`_reuse` 和 `probe` 都没有重新拉取的路径。

## 改动
- `github.py`：把 PR 发布用的 `_git_env` 提取为 `github_git_env(token)`，`GitHubDelivery` 继续调用它，行为不变。
- `maintenance_subsystem.py`：
  - 凭据：github.com 的 HTTPS 地址使用平台 GitHub 凭据，走同一套认证：只作用于 `http.https://github.com/.extraheader`，通过环境级 git 配置传入，关闭全局和系统 git 配置与 hooks，不进 URL、argv、日志或持久配置。其他主机与 SSH 保持原样。
  - `credential_ref`：只接受 `github`。未知名称，或 `github` 配非 github.com 地址，登记时 422；旧记录里存了未知引用的，重试时失败（`credential_unknown`），不克隆。
  - 重试：`probe`（页面上的“重新接入”）和重新登记同一 URL，都会对克隆失败的仓库重新克隆，复用原项目 ID 和原工作区。先克隆到临时目录再移入；目标非空时不覆盖；同一项目在同一进程内只跑一次克隆，克隆进行中再次探测只返回当前状态；进程崩溃后残留的 `analyzing` 状态可以重试。
  - 失败回执：`access.reason/message/next_step/detail/credential`，`detail` 已脱敏（去掉 token、Authorization 头、URL 中的 userinfo）。
- CLI 的 `repo probe` 改为同步执行（`background=False`）。
- 前端 `ReposPage`：列表里直接显示失败原因、下一步、可展开的脱敏详情，以及“重新接入”按钮；详情页显示下一步，失败时按钮改名为“重新接入”；登记表单说明“GitHub 仓库留空即使用平台凭据”。
- 契约：`docs/maintenance-subsystem/CONTRACT.md` 在 repos 条目下补充了凭据、失败和重试规则。

## 验证（定向，没有调用付费模型）
- `pytest tests/test_maintenance_repo_auth.py tests/test_maintenance_subsystem.py`：22 passed。其中新增 8 条，只假造 `git clone` 这一个调用，其余 git 调用都真实执行。覆盖：无 token 时给出原因和下一步；有 token 时 argv 和 URL 里没有 token，env 使用 github.com 的 extraheader，克隆后 `.git/config` 里没有 token；失败后重新接入沿用同一项目；重新登记会重新克隆；未知或用错主机的引用在登记时被拒；存量未知引用重试失败且不克隆；非空目标不覆盖且不留临时目录；三次并发重试只克隆一次；脱敏。
- 与 GitHub 发布和维护 CLI 相关的 6 个测试文件：88 passed。
- 前端：`tsc` 通过，`vitest src/maintenance` 5 个文件 40 条通过（新增 1 条：失败原因、下一步、重新接入），`npm run build` 通过。
- 编写过程中，并发测试抓到一个真实问题：克隆进行中再次探测会走本地探测，把状态改写成 failed。已修复。

## 复核修正：重试丢分支（Codex 读 064f297）
- 问题：首次克隆把 `branch` 传给了 `_start_clone`，但重试不传，于是拉默认分支并覆盖 `base_branch`。
- 修法：登记时保存 `requested_branch`（`null` = 未指定）；每次重试从记录读取；克隆完成后 `base_branch` 取实际克隆到的分支。克隆尚未成功时，重新登记可以纠正分支；已有工作区时，指定不同分支返回 422，不切换。旧记录没有这个字段：`base_branch` 不是 `main` 就视为显式选择，是 `main` 就视为未指定。
- 回归：FakeClone 现在原样保留 `--single-branch --branch` 等参数，只把 URL 换成本地仓库，所以克隆到哪个分支是真实结果。新增两条：非默认分支首次失败后重试，HEAD 和 SHA 都在 release 上；分支纠正只在尚未克隆成功时生效，已有工作区拒绝切换。`test_maintenance_repo_auth.py` 与 `test_maintenance_subsystem.py` 共 24 passed。

## 上线后在现场怎么验
1. 在列表里对 group-risk 点“重新接入”（或调用 `POST /api/v2/maintenance/repos/<原 project_id>/probe`）。预期：沿用同一个 project_id，状态变为分析中，然后是待补充或可开始维护。
2. 如果仍然失败：列表会直接显示 reason 和下一步。`auth` 且 `credential=github` 表示平台账号对该仓库没有权限。

## 未验证 / 限制
- 没有对真实 GitHub 做过克隆（按要求不读凭据、不访问服务器）；真实结果以 Codex 上线后的验收为准。
- 并发去重只在单个进程内有效（服务本身是单进程部署）。
- 只支持 `github` 这一个凭据名；其他托管平台的凭据需要另行设计。
