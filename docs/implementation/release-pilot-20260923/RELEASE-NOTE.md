# webuddy-maintenance-2026-09-23

已上线提交 `de4df07`（上一版 `363da96`），线上地址 https://harness.cloudwaveai.cn/maintenance 。

- **入口**：网页 `/maintenance`（代码库登记、需求接入、维护任务、监控汇总与关系画布、嵌入入口）。接口契约见 `docs/maintenance-subsystem/CONTRACT.md`，安装见 `INSTALL.md`。
- **CLI**：`webuddy-maintenance`（`pyproject.toml` 的 entry point `factory.control.maintenance_cli:main`）；线上主机上在 `/home/ubuntu/releases/factory/de4df07/bin/webuddy-maintenance`。
- **交付形态**：补丁包 + 检查结果，不自动部署。
- **未接入**：CPU/内存/服务探针与告警采集。没有真实客户仓库跑过全链路（仅合成仓库验收）。
- 发布证据：`/Users/auntlee/Desktop/自动化harness构建/docs/releases/2026-09-23-maintenance-de4df07/`。
