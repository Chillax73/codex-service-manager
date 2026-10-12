# 验收证据索引

状态：A-01 至 A-16 本机验收通过（2026-10-12），实现与资源清理完成。环境：Apple Silicon、macOS 15.7.3；Codex 目标 com.openai.codex。

发布版结果见 [脱敏报告](../../docs/results/VALIDATION.md)和 [机器可读摘要](../../docs/results/validation-summary.json)。下表原始 JSON/截图是本机复验来源，因含个人路径和运行元数据不上传 GitHub；文件名用于定位本机证据，不代表远端附件。

| 用例 | 当前结果 | 权威证据与覆盖边界 |
|---|---|---|
| A-01 | 配套入口通过 | project-tests.json：当前桌面工具使用 scripts/csm，cwd 自动登记；实际 UI 显示归属。普通未经入口启动的进程仅发现。 |
| A-02 | 通过 | project-tests.json：npm/Vite、Maven/Spring、Python WSGI 实际就绪与重启。 |
| A-03 | 通过 | test_engine.py test_05：进程存活且 URL 不可达，分别显示 running / unavailable。 |
| A-04 | 通过 | test_01、test_09、project-cleanup.json：子进程停止、端口释放、其他项目隔离。 |
| A-05 | 通过 | 真实项目 cwd/env 与新运行记录；ui-tests.json 实际重启、历史日志、外部停止后历史自动刷新。 |
| A-06 | 通过 | test_03/04、docker-failures.json：冲突不认领原对象，不存在命令和真实容器启动失败保存诊断与历史。 |
| A-07 | 通过 | test_02 与 ui-batch-partial.json：出生身份不符拒绝信号，原进程仍存活。 |
| A-08 | 通过 | test_06：成功/失败/取消/重跑；实际 UI 任务重跑、表单环境覆盖与退出码。 |
| A-09 | 通过 | docker-tests.json、ui-container-state.json、ui-tests.json：实际 UI 单容器控制，同一 ID、新记录、marker 保留、动态 URL 与 stderr 日志。 |
| A-10 | 通过 | docker-tests.json、ui-compose-single-state.json、ui-compose-group-state.json：实际 UI 成员隔离、整组重启逐项仅一次、数据保留。 |
| A-11 | 通过 | test_09 四种配置；ui-policy-state.json 实际 UI 手动结束、两个开关、keep 例外与项目隔离。实际 Codex 触发另列 A-13。 |
| A-12 | 通过（含人工观察） | 管理窗口关闭/重开无误停；codex-chat-events.json 实际桌面 API 切换/归档后同一运行存活；用户明确确认实际关闭 Codex 窗口再打开，UI 前端仍运行，见 real-codex-quit.json human_observation。 |
| A-13 | 通过（实际 Codex） | real-codex-quit.json：原 Codex PID/出生身份消失，正式观察器四项 codex_exit 停止成功，同次运行 ended；keep 与其他项目保持原运行，观察器独立存活。用户确认 UI 前端停止。未合成退出事件。 |
| A-14 | 通过 | ui-manager-recovery.json：实际 Cmd-Q 后 UI 进程消失、观察器与同一运行存活，原生重开恢复；ui-tests.json 与 ui-latest-window.png 记录实际窗口。 |
| A-15 | 通过 | test_07/08/10/13：锁、去重、停止失败禁止重启；ui-batch-partial.json 和实际 UI 提示证明逐项部分失败及其他项目隔离。 |
| A-16 | 通过 | docker-unavailable.json、test_12；真实 Docker 引擎关闭 UI 显示未知/诊断并恢复；ui-watcher-recovery.json 真实观察器中断、界面提示、点击恢复后的新 PID/心跳。 |

文件均位于 ../../docs/evidence/。ui-tests.json 的 28 项断言来自实际原生界面；基础进程、容器和契约测试分别记录，不混为一种证据。

系统菜单栏详情入口由用户实际操作并确认可用。快捷停止/重启共用已通过主窗口验收的控制链，但没有逐个重复人工点击的证据；保留该覆盖边界。人工观察与自动原生 UI/实际进程记录分别标注，不把源码或主窗口截图冒充状态菜单证据。

最终核验完成：退出后四项停止操作逐项成功且各仅一次，两项例外未换运行。ui-cleanup.json 记录测试拥有的 10 项服务归档/登记清理、3 个容器和 1 个卷移除、端口释放、原有九个容器状态/启动时间不变、Docker 恢复关闭；正式观察器保留。T06/T08/T10 已完成。

## 开源收尾验收（2026-10-12）

O02：核对直接依赖官方 LICENSE、锁文件间接依赖和源码/图片来源，记录范围而非假称第三方审计。O03：在不包含 ignored 工作目录的新源码副本运行基础测试；安装依赖后使用 PATH 运行真实项目、停止并核对测试资源。O04：记录 GitHub CI 和手动 Docker workflow 链接及实际结果。O05：仅测试数据的真实窗口截图、逐条检查 README 命令与文档链接。O06：安全入口存在且禁止上传私密数据。O07：GitHub 预览 release 的 tag/commit 与验证通过的源码一致，不附未公证应用。

O02–O06 本机与远端检查通过，逐项证据见 [开源核验](../../docs/results/OPEN_SOURCE_READINESS.md)。仓库在发布准备期间由用户改名为 codex-service-manager，仓库 ID 与独立脱敏公开快照一致；所有对外地址已同步。

O07：v0.1.0 已发布为 prerelease，注解标签指向通过 CI 的 6a52478，tagger 使用 noreply，未包含应用附件；见发布页与开源核验。
