# 服务器连接管理

管理员在运行配置中注册目标并布置公钥，再在项目页显式绑定。成员可在已分配项目发起 release；默认只做准备，勾选“执行部署”才授权平台在独立验收通过后执行部署。服务器连接不属于模型工具，不支持交互终端。

## 配置与信任

服务进程配置 `FACTORY_DEPLOY_KEY_DIR` 为绝对路径，例如 `/home/webuddy/.webuddy-deploy-keys`。目录必须在数据目录和整个工作区根目录之外，禁止符号链接，目录权限 0700，归服务进程用户所有。未配置时注册目标返回明确阻碍，已有任务不受影响。每目标生成独立 Ed25519 密钥，私钥文件 0600、不入数据库、不进入 API；公钥和其 SHA256 指纹用于布置授权。

管理员从可信渠道核对服务器主机公钥的 SHA256 指纹后录入。平台使用 ssh-keyscan 取得公钥，但扫描结果本身不被信任：只有匹配录入指纹的公钥才写入本次调用的专用 known_hosts。SSH 强制 StrictHostKeyChecking=yes，同时关闭密码、交互认证、代理、配置文件继承、连接复用与转发。指纹不匹配会拒绝连接并经可选告警通道通知。

注册页“测试连接”只运行固定的 `true` 认证探针，不执行任何注册的业务命令；这不证明部署脚本或服务健康。修改目标使用 revision 乐观锁；一个项目最多绑定八个目标，每个目标可供多个项目使用。绑定修改也有自己的 revision。删除前先解除全部绑定，删除时一并移除私钥。密钥目录需备份到受控位置；移动服务时保留其属主及权限，平台不提供私钥导出。

## 五个动作

| verb | 管理员注册值 | 调用参数 |
| --- | --- | --- |
| health_check | 固定命令或 HTTP(S) URL；URL 在目标服务器上用 curl 请求 | 无 |
| service_status | 固定状态命令 | 无 |
| fetch_log | 绝对日志路径 | lines，1–500，默认 100 |
| deploy | 已有部署脚本的固定命令 | 无 |
| rollback | 已有回滚脚本的固定命令 | 无 |

空值保持未配置。固定命令不能包含明文凭据；凭据放在服务器端受限配置中。部署脚本由管理员在目标服务器维护，平台不生成、不修改脚本，不传输产物；脚本自行拉取已发布产物。

每次 release 提交记录绑定目标的 ID、名称、revision，不把地址、登录用户、命令或密钥路径加入提示词。目标配置变化后旧运行拒绝操作，需新建运行。用户的 `execute_deploy` 布尔值参与请求指纹，只有 release 接受 true；同幂等键改变授权返回 409。

远程写操作统一检查：项目仍绑定目标、目标版本相符、release 工作流、用户显式勾选、独立验收 pass 且已进入待评审/已发布状态。缺一即 409。模型可在验收结构化结果里声明最多八条 `remote_requests`，字段仅为 target_id、verb 和 fetch_log 的 lines。模型无任何 SSH 工具，也不能提供命令；平台同样校验这些请求。默认 release 完成后自动探测健康与状态，勾选部署时先执行部署脚本；回滚不会自动发生，只能由符合上述条件的显式平台调用或模型结构化请求触发。

## 超时、幂等与证据

单个动作默认总时限 30 秒，包括指纹扫描；SSH ConnectTimeout 为 5 秒。只读动作遇到 SSH 连接错误最多重试一次，写动作不自动重试。超时终止整个本地进程组；远程服务端脚本是否已经完成或停止不能据此保证，因此写动作回执未知时必须人工核对。输出最多接收 256 KiB，超限停止并丢弃原始输出；正常输出先经 factory.redact 脱敏，再截摘要至 12000 字符，日志另限制行数。

写操作在 I/O 前以 run_id、target_id、verb 持久登记。并发、重试或服务重启都不能重复执行同一写动作。回执未收到保留 unverified，运行详情从持久回执补齐展示；不可把未知状态当作成功，也不可盲目重放。管理员核对后可另行发起运行。

每次调用记录 remote.executed（目标名/ID、verb、退出码、耗时、脱敏摘要；复用回执标记 reused）。连接失败、超时、非零退出均为该动作 unverified，不使本地验收变为 fail。deploy/rollback 失败及主机指纹不匹配进入现有通知 outbox；未配飞书则不发，通知发送失败不影响执行。目标注册/编辑/删除/测试写 deploy_target_audit，项目绑定写 project_settings_audit。

## 可选远程巡检

巡检本身与“远程只读探测”都默认关闭。开启远程选项后，入队时冻结目标版本，平台只调用 health_check 和 service_status，结果保留在运行证据与 inspection_history。远程未验证使该次历史结论为 unverified，仍保留本地验收事实，不触发自动修复、部署或回滚；历史旧数据不回填。

## 威胁模型与服务器侧限制

- 仓库提示注入：预注册命令和平台权限校验限制模型输出。部署授权是用户表单的结构化字段，不从模型文字推断。
- 输出泄密：落库前统一脱敏；脱敏无法识别所有未知格式的秘密，服务器端脚本也应避免输出凭据。
- 横向移动：每目标不同密钥、固定主机指纹；密钥目录在 Linux/macOS worker 沙箱中不可读，SSH_AUTH_SOCK 不传入命令子进程。
- 平台主机失陷：0600 不抵御服务账号或 root 被攻破。部署账号只授予所需权限，首版不提供 KMS/HSM。

建议目标服务器使用专用低权限账号及 ForceCommand（不强制）。在 authorized_keys 对本平台公钥加限制，例如：

```text
restrict,command="/usr/local/libexec/webuddy-restricted" ssh-ed25519 <平台生成的公钥>
```

下面只是服务器端白名单包装器示例，不是部署脚本。管理员须让其中命令与目标注册值严格一致，并由 root 管理包装器及其调用脚本，禁止部署账号修改这些文件；根据实际系统安装绝对路径。不使用 eval，不拼接客户端输入。

```bash
#!/bin/bash
set -eu
case "${SSH_ORIGINAL_COMMAND:-}" in
  true) exit 0 ;;
  /usr/local/libexec/myapp-health) exec /usr/local/libexec/myapp-health ;;
  'systemctl status myapp --no-pager') exec /usr/bin/systemctl status myapp --no-pager ;;
  /usr/local/libexec/myapp-deploy) exec /usr/local/libexec/myapp-deploy ;;
  /usr/local/libexec/myapp-rollback) exec /usr/local/libexec/myapp-rollback ;;
esac
if [[ ${SSH_ORIGINAL_COMMAND:-} =~ ^tail\ -n\ ([0-9]{1,3})\ --\ /var/log/myapp.log$ ]]; then
  lines=$((10#${BASH_REMATCH[1]}))
  if (( lines >= 1 && lines <= 500 )); then
    exec /usr/bin/tail -n "$lines" -- /var/log/myapp.log
  fi
fi
printf '%s\n' 'Command not allowed' >&2
exit 126
```

## 首版明确不做

密码/交互认证、任意远程命令入口、文件上传下载、多跳/堡垒机、KMS、Windows 目标均不支持。绑定服务器不会自动扩展项目权限或授予模型 SSH。
