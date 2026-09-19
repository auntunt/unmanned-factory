# 同机 Docker 首次交付决定 · 2026-09-19

用户确认：测试应用暂用当前 webuddy 服务器；固定 IP/端口即可，域名/DNS/证书不阻塞；优先 Docker。目标是走通开发、GitHub、部署、实际操作、第二轮同地址更新。平台自身 RC1 切换尚未执行，不因应用演示默认升级平台。

## 实际环境

- 当前主机 ubuntu@43.153.76.85，x86_64。不是用户历史规范里的 8.141.121.22。
- Docker 29.6.2 可通过现有 sudo 权限使用；ubuntu 默认无 docker.sock 权限，未为模型开放 Docker socket。
- 入口服务 Caddy active，Nginx inactive；不套用历史 Nginx conf 或证书命令。
- 原 New API 容器占 3000，webuddy 127.0.0.1:8788；演示独立名 webuddy-demo-counter / 18081。
- 起始磁盘约5GB空余，内存约2GB可用。示例采用 python:3.12-alpine，128MB/0.5CPU/64pids上限，避免安装大型依赖。

## 运维接线

现有平台 deploy-target API + 项目绑定 + 受控 SSH 动作。管理员脚本 /usr/local/libexec/webuddy-counter-deploy，状态与版本 /opt/webuddy-demo-counter，根目录不挂载进容器。

固定脚本按指定 GitHub commit 拉取私有源码、构建、替换独立容器、检查 health/version，保存前一镜像与部署回执。容器只获得版本变量，不获得GitHub或SSH凭据。模型只能开发项目；服务端发布流程调用预注册动作。

每次交付选定已发布 commit 是本次验收运维接线的一部分，尚不能据此宣称 webuddy 已自动生成所有项目的部署模板。受控连接验证通过不等于应用部署成功，最终以实际访问和版本校验记录为准。

## 现场观察（不抹去）

1. 验收脚本第一次错误地对已结束run调用follow-up，平台正确409；随后改用界面同款的新一轮run入口。属于探针接线错误。
2. 模型首次需求分析 flows 返回对象而非字符串列表，平台进入needs_human。保留原始记录，经既有恢复入口重试后规格通过；不是一次输入无人闭环。
3. 后续预检短暂报claude SDK API unavailable，独立同环境探测得到SDK0.2.152兼容/可执行。通过既有clarify恢复继续；不将其假称已定位修复。
4. 验收探针曾在awaiting_approval观察到后关闭隔离服务，后续读取时执行已经继续并遇到SDK预检错误；保留各次状态，不能把探针早退称作生产停机故障。

最终执行结果另见本目录验收报告；本文只记录决策与环境，不宣称通过。
