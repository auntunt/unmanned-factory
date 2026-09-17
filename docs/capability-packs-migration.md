# 职能包生命周期：迁移与接口说明

对应设计包 `docs/design/capability-lifecycle-v1`（architecture.md 第 8 节 A→E）。
基线 4c56632。本文写的是**已实现**的部分与迁移方式，未完成项在最后一节。

## 1. 数据库迁移

全部为新增表，**不改写任何既有行**，也不改既有表结构。表在 `PackStore(store)` 构造时
用 `CREATE TABLE IF NOT EXISTS` 建立（router 工厂里调用一次），旧库直接启动即可完成迁移，
无需停机脚本、无需回填。

| 表 | 用途 | 不可变性 |
|---|---|---|
| `capability_packs` | 职能包身份（名称、用途、维护者、来源任务） | 可更新 |
| `pack_drafts` | 候选内容（revision 乐观锁、内容摘要、工具契约、排除清单） | 可更新 |
| `pack_blobs` | 按 sha256 寻址的内容存储，草稿与版本共用 | 只增 |
| `pack_versions` | 已发布的不可变版本 | **触发器禁止 UPDATE/DELETE** |
| `pack_evaluations` | 验证证据，绑定候选内容摘要 | **触发器禁止 UPDATE** |
| `pack_env_checks` | 每个版本在本机的环境可用性 | 可覆盖（带 checked_at） |
| `pack_bindings` | 职能体 → 具体版本的挂靠 | 可更新（revision 乐观锁） |
| `pack_tasks` / `pack_task_events` | 受控调用任务与可续读事件 | 事件**触发器禁止 UPDATE** |
| `pack_artifacts` | 输入/输出文件，按所有者鉴权 | 只增 |
| `pack_operations` | (actor, operation_key) 幂等登记 | 覆盖写同键同内容 |

旧记录没有新元数据时不会被批量标绿——没有版本的包在目录里显示为「草稿」，没有环境检查的
版本显示为「尚未检查」。既有 `agents / agent_versions / agent_manifests / capabilities / runs`
完全未被触碰。

### 回滚（常规）

**切回旧 release，保留新增表。** 旧版本的代码不认识这些表，也不会读写它们；表留着不影响
任何旧路径。这是本次授权范围内的回滚动作：切换 release、重启服务，不动数据库。

**不要在回滚时删表。** 删表会连同已发布的不可变版本、验证证据、挂靠关系和调用产物一起丢掉，
而这些正是回滚要保护的东西。破坏性迁移（删表 / 清数据）是一次单独的维护动作，需要单独授权
与单独的备份确认，不属于本次回滚。

## 2. 新增 API（全部需登录，服务端校验权限）

前缀 `/api/v4/capability-packs`（核对过现有路由：`/api/v3/capabilities` 是沉淀方法库，
两者是不同实体，互不影响）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `` | 目录。未发布的候选只有维护者/管理员可见 |
| GET | `/{pack_id}` | 详情（草稿、版本、证据、挂靠、环境）；非维护者看不到草稿 |
| GET | `/{pack_id}/files/{path}` | 读取包内文件（维护者） |
| POST | `/drafts` | 从开发任务成果建立候选（`source_run_id` + 显式选取 + `operation_key`） |
| PUT | `/{pack_id}/draft` | 更新候选内容（`expected_revision` 乐观锁；改内容即让验证失效） |
| POST | `/{pack_id}/evaluations` | 运行验证（耐久 job，真实执行包内测试集） |
| GET | `/jobs/{job_id}` | 验证作业状态（只有发起者/管理员可读） |
| POST | `/{pack_id}/versions` | 发布不可变版本（事务内复核权限/摘要/证据/契约） |
| POST | `/versions/{version_id}/environment-check` | 记录环境可用性（与发布状态分离） |
| GET | `/bindings/{agent_id}` | 职能体的挂靠，含 `upgrade_available` |
| POST | `/bindings` | 挂靠 / 升级 / 回滚（`expected_revision`） |
| DELETE | `/bindings/{agent_id}/{pack_id}` | 解除挂靠 |
| POST | `/invocations` | 上传文件并调用（multipart；冻结版本 + 角色版本） |
| GET | `/invocations` · `/invocations/{id}` · `/invocations/{id}/events?cursor=` | 任务与可续读事件 |
| POST | `/invocations/{id}/cancel` | 取消调用：先落库再置作业取消位，运行时回收整个进程组 |
| GET | `/artifacts/{id}/download` | 成果下载，响应头带 `X-Validation-Status` |

`operation_key` 规则：同一 actor 同键同内容 → 重放首个结果；同键不同内容 → 409。

## 3. 执行约定

工具是普通程序，不是模型：固定入口、stdin/stdout JSON、超时、CPU/输出大小/句柄上限、
只读 `input/`、只可写 `output/`、无网络、不继承任何凭据环境变量。

**没有可验证的隔离就不执行。** `pack_sandbox.probe()` 每个进程真跑一次金丝雀：在沙箱里尝试
写沙箱外、读宿主文件、建立对外连接，三件事都必须失败才算隔离可用。探测不通过时 `run_tool`
直接以 `isolation_unavailable` 拒绝，`environment_report` 把该版本标为 `unavailable`——
不存在「记一条 sandbox: none 然后照跑」的路径。

| 平台 | 后端 | 保证 |
|---|---|---|
| macOS | Seatbelt（`sandbox-exec`），默认拒绝 | 只读系统路径与运行时 + 本次工作目录；只可写 `output/` 与私有 tmp；`deny network*`；宿主用户文件读不到 |
| Linux | bubblewrap（`bwrap`），`--unshare-all` | 网络命名空间隔离（不是靠策略语句禁网）；只 ro-bind 运行时与必需系统库，`/home`、`/root`、`/var`、`/opt`、`/etc` 里的凭据在沙箱里**不存在** |
| 其他 / 探测失败 | 无 | 拒绝执行，环境标 `unavailable` |

**Linux 部署前提**：装 `bubblewrap`，并允许非特权 user namespace（Ubuntu 24.04 的
`kernel.apparmor_restrict_unprivileged_userns=1` 会拦掉）。金丝雀跑不通就没有工具执行能力，
这是有意的阻断。

资源上限只作用于工具子进程：CPU 秒、单文件与总输出上限、文件句柄数，能设内存上限就设。
**能力探测一律在短命子进程里做**，服务进程的 rlimit 一个字节都不动（早先版本在模块导入时
先降后升，Linux 上恢复会抛 `ValueError: not allowed to raise maximum limit`，等于把服务
进程永久降级）。本机设不了内存上限时如实记 `unsupported_on_host`。

依赖声明**只解析、不执行**：按 PEP 508 解析，再查 `importlib.metadata` 里已安装的版本与
版本约束；不 import 被声明的包，不把清单字符串拼进任何可执行语句。Python 版本约束按完整
版本比对（`3` → `>=3`，`3.12` → `==3.12.*`，也接受完整 specifier）。

工具契约的 `input_schema` / `output_schema` 由 `jsonschema`（Draft 2020-12）先自检再校验
实例，远端 `$ref` 一律拒绝；`outputs` 里的畸形元素变成结构化问题，不会抛异常把任务卡死。
stdout 边读边限流，超过 256 KB 立即回收整个进程组，不会先无界缓冲再判大小。

**任务与作业原子关联**：`pack_tasks` 在建表的同一个事务里写入 `job_id`。派发前崩溃留下的
任务，重启时由 `recover_interrupted()` 对账到明确终态（`failed` / `error_code=interrupted`），
不会永远停在「处理中」；同一操作键的重试只有在**从未真正派发**时才补派发，已完成的重放
直接返回原结果，不会把同一个文件再转一遍。取消是持久化意图，落库后再置作业取消位，
超时与取消都按进程组回收（只杀直接子进程会留下孤儿）。

**验证提交即幂等**：操作键在提交时登记，重复提交回放同一个作业，不会把测试集再真跑一遍；
同键不同候选内容 → 409。

**环境检查会过期**：解释器/平台指纹变化或超过 24 小时，状态退回 `unchecked` 并给出原因，
不让一次 `ready` 永久代表「现在能跑」；发布状态不受影响。

**角色版本与能力版本在同一个事务里冻结**：`create_task` 在自己的事务内直接读 `agents` 行。

工具「声称成功但没有产出」判失败（`validation_failed`）。文件生成但校验不过仍是 `failed`，
候选产物可下载并标「未通过验证」。

## 4. 独立可复现命令

```bash
python3 -m pytest tests/test_capability_packs.py tests/test_mfd_xml_pack.py \
                 tests/test_pack_hardening.py tests/test_pack_api_durability.py \
                 tests/test_codex_pack_review.py -q -m "not smoke"
cd frontend && npx vitest run src/workbench/PackDetail.test.tsx src/conversation/CapabilityPanel.test.tsx
cd frontend && npm run build
python3 factory/control/builtin_packs/tools/mfd-xml/fixtures/make_fixtures.py   # 重放合成样本
python3 -c "from factory.control.pack_sandbox import probe; print(probe().as_dict())"  # 本机隔离金丝雀
```

`tests/test_base_install.py` 与 `tests/test_control_providers.py` 里有几条用例会起
`python -I` 子进程去 import `factory` / `openai_codex`——它们要求**本包已安装**（`uv sync`
或 `pip install -e .`）且装了 `codex` extra。只用 pytest 的 `pythonpath=["."]` 跑仓库时这几条
必失败，与执行顺序无关（`-I` 会忽略 `PYTHONPATH` 和当前目录）。

## 5. 新增运行时依赖

`[project].dependencies` 增加两项。`uv.lock` 只改了本项目自身的依赖列表（4 行）——这两个包
此前已作为传递依赖存在于锁文件中，没有引入新的解析结果，也没有改动任何既有版本。

| 依赖 | 用途 |
|---|---|
| `jsonschema>=4.20.0` | 工具契约 schema 自检与输入/输出实例校验 |
| `packaging>=23.2` | 依赖声明（PEP 508）解析与版本约束比对 |

## 6. 未完成项

- **E（线上验收）未做**：测试账号上的升级/回滚/取消/重启恢复/实际下载复测由 Codex 在
  独立复核与部署流程中进行。本轮不部署。
- **Linux 隔离本轮没有在 Linux 主机上实跑过**：本机是 macOS。bwrap 分支的命令行构造有单元
  覆盖，金丝雀逻辑与 macOS 共用，但「金丝雀在真 Linux 上通过」必须在部署机上验证。
  金丝雀不通过时服务拒绝执行工具，不会退化成裸跑。
- 职能包只能由**开发任务成果**创建（`source_run_id` + 已保存的成果清单）。从独立会话产物
  建包、以及包的导入/导出，尚未实现。
- 环境检查只覆盖解释器与依赖包 import，不检查系统级依赖。
- `waiting_input` 状态已在模型里定义，但当前工具契约不需要中途补输入，没有触发路径。
- MFD 能力的边界见 `docs/mfd/mfd-xml-extraction-report.md` 第 5 节。
