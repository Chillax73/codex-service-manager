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
- [ ] O04 基础 CI 与独立 Docker 工作流实际执行。
- [x] O05 README、真实脱敏截图、卸载与排障。
- [x] O06 CONTRIBUTING/SECURITY 与 Issue/PR 模板。
- [ ] O07 CHANGELOG、v0.1.0 源码开发者预览。
