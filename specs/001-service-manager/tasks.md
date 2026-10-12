# 实施任务

- [x] T01 需求映射、设计、默认值与接口文档。
- [x] T02 统一 SQLite 模型、持久化与独立本机监督器。
- [x] T03 进程身份、健康、任务、日志滚动、失败与并发防护。
- [x] T04 Docker/Compose 适配、组成员、历史、数据保留。
- [x] T05 项目策略、服务例外、操作结果与批量控制。
- [x] T06 原生退出观察器、组件状态、误触发排除。
- [x] T07 CLI/项目配套接入、实际桌面自动登记证据。
- [x] T08 原生主窗口、菜单栏、详情/日志/历史/配置/发现。
- [x] T09 .app 构建、启动说明、恢复与升级路径。
- [x] T10 A-01 至 A-16 实际验收，修复问题，记录限制。

任务完成必须附证据，不因文件已经存在而自动勾选。

## 2026-10-12 完成证据

T02–T05：process-tests.json、project-tests.json、docker-tests.json 和 docker-unavailable.json。
T07：接入安装器、当前项目 AGENTS.md、真实桌面配套启动与原生窗口截图。
T09：build/Codex Service Manager.app 可运行、签名校验和 minos 13.0、README/接入说明。
T06：real-codex-quit.json 证明真正 Codex 进程退出、四项停止、两项同一运行保留、正式观察器继续运行；用户确认关闭 Codex 窗口仍运行。观察器真实故障/恢复见 ui-watcher-recovery.json。
T08：ui-tests.json 的 28 项实际原生断言与 ui-latest-window.png；用户确认系统菜单栏详情入口可用，见 real-codex-quit.json 的 human_observation。未将菜单栏每个快捷动作分别冒充已人工点击。
T10：A-01 至 A-16 证据映射见 acceptance.md；ui-cleanup.json 保存归档、精确资源清理和原有九个容器未变的核验。

补充回归：14 组本机进程/历史日志、6 项真实项目、15 项 Docker/Compose、8 项容器失败、9 组契约、28 项实际界面断言。源代码断言和覆盖边界与各证据文件对应；无需用重复引擎测试代替已取得的桌面操作证据。

独立记录任务已成功结束。测试进程/容器/卷和本轮登记已清理，历史归档到 ui-cleanup.json；正式观察器与原有记录保留，Docker 恢复验收前关闭状态。

## 开源收尾

- [x] O01 独立公开 Git 历史和 MIT。
- [x] O02 依赖/来源授权清单。
- [x] O03 全新克隆目录与工具查找、安装步骤、隔离状态。
- [x] O04 基础 CI 与独立 Docker 工作流实际执行。
- [x] O05 README、真实脱敏截图、卸载与排障。
- [x] O06 CONTRIBUTING/SECURITY 与 Issue/PR 模板。
- [x] O07 CHANGELOG、v0.1.0 源码开发者预览。

O02–O06 证据：[开源核验](../../docs/results/OPEN_SOURCE_READINESS.md)、依赖清单、真实测试截图；GitHub macOS CI 与独立 Docker 集成都已实际通过。

O07：[v0.1.0 开发者预览](https://github.com/Chillax73/codex-service-manager/releases/tag/v0.1.0) 已发布，标签 6a52478、CI run 38224570507 通过；首版仅源码，不附未公证应用。

## 文档补充

- [x] D01 README 与 hooks 使用指南：正式机制/可选实验职责，安装、审核、核验、数据与移除边界。

D01：README 的“Codex Hooks 的作用与使用”、docs/HOOKS.md 与接入/原型文档已对应；命令/事件对照源码和文档链接检查通过。仅文档更新，未安装 hooks、修改信任或合成桌面事件。

## 项目管理可用性

- [x] U01 项目重命名/删除规格、接口与安全边界。
- [x] U02 后端和 CLI：校验、活动对象阻止、事务与历史隔离。
- [x] U03 原生入口、表单、预览和删除后选择恢复。
- [x] U04 真实进程/容器集成、构建与实际窗口操作证据。

U01–U04：13 组项目契约、14 组真实进程、8 项真实容器、18 项实际原生 UI 与构建已通过，见 docs/results/PROJECT_MANAGEMENT.md；恢复支持撤销误删，回收摘要不暴露环境。
