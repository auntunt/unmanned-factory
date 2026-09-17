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

**回滚**：删除上述表即可回到迁移前状态；既有 `agents / agent_versions / agent_manifests /
capabilities / runs` 完全未被触碰。旧记录没有新元数据时不会被批量标绿——没有版本的包在目录里
显示为「草稿」，没有环境检查的版本显示为「尚未检查」。

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
| GET | `/artifacts/{id}/download` | 成果下载，响应头带 `X-Validation-Status` |

`operation_key` 规则：同一 actor 同键同内容 → 重放首个结果；同键不同内容 → 409。

## 3. 执行约定

工具是普通程序，不是模型：固定入口、stdin/stdout JSON、超时、CPU/输出大小/句柄上限、
只读 `input/`、只可写 `output/`、默认无网络。macOS 上用 Seatbelt 落实「禁网 + 只可写 output」；
**本机不支持的限制如实记为 `unsupported_on_host`**（实测 macOS 拒绝下调 RLIMIT_AS），
没有沙箱时证据里写 `sandbox: "none"`，不冒称已隔离。

工具「声称成功但没有产出」判失败（`validation_failed`）。文件生成但校验不过仍是 `failed`，
候选产物可下载并标「未通过验证」。

## 4. 独立可复现命令

```bash
python3 -m pytest tests/test_capability_packs.py tests/test_mfd_xml_pack.py -q -m "not smoke"
cd frontend && npx vitest run src/workbench/PackDetail.test.tsx src/conversation/CapabilityPanel.test.tsx
cd frontend && npm run build
python3 factory/control/builtin_packs/tools/mfd-xml/fixtures/make_fixtures.py   # 重放合成样本
```

## 5. 未完成项

- **E（线上验收）未做**：测试账号上的升级/回滚/取消/重启恢复/实际下载复测由 Codex 在
  独立复核与部署流程中进行。本轮不部署。
- 职能包只能由**开发任务成果**创建（`source_run_id` + 已保存的成果清单）。从独立会话产物
  建包、以及包的导入/导出，尚未实现。
- 环境检查只覆盖解释器与依赖包 import，不检查系统级依赖。
- `waiting_input` 状态已在模型里定义，但当前工具契约不需要中途补输入，没有触发路径。
- MFD 能力的边界见 `docs/mfd/mfd-xml-extraction-report.md` 第 5 节。
