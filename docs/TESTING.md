# 复现与 CI

## 基础回归（无需第三方 Python 包）

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_support.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_endpoints.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_engine.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_projects.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_capture_shell.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_maintenance.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_capture.py --suite core
/usr/bin/python3 scripts/check_release.py
./scripts/build.sh
```

入口自动创建 ignored `.test-state/` 与 `docs/evidence/`。进程验收启动真实测试服务，需要本机进程与端口权限，只清理自己的服务，保留本机诊断。原始诊断不上传。

## 项目栏、扫描策略和服务配置

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_migrations.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_observation_policy.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_project_ordering.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_service_ordering.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_service_settings.py
```

桌面复验使用单独的 `--root` 和应用偏好域。新窗口核对项目栏宽度，再拖动、折叠、缩窄窗口并退出重开。用不同创建时间的测试项目核对置顶分组和排序，用测试服务核对单项、多选和全量暂停、单项目及全部服务刷新、服务排序、配置生效和删除后的重新采集。详见 [0.0.3 体验优化验收](results/UX_0_0_3.md)。

## 用户级自动观察（开发分支）

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_capture.py --suite core
# 完整项目套件需先按下文准备 npm/Maven 夹具：
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_capture.py --suite full
# Docker/Compose 可用且 python:3.12-slim 已存在时：
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_capture_docker.py
```

进程入口使用独立临时状态，启动真实采集器及测试进程，需要本机进程/端口权限。现含 22 组，包括分项目暂停测试及原有采集、普通接管/配置、Vite、离线 Maven/Spring、多服务/已登记任务隔离、身份/超时/防副本及独立 RPC 状态目录。容器入口使用唯一测试标签、已有镜像，比较原有容器状态，现含 7 项检查。hook 输入为显式模拟，不能当作真实桌面回调。原生 UI 的实际操作及独立进程核验见 adoption-ui-tests.json，综合结果见 [普通启动接管](results/ORDINARY_ADOPTION.md)。macOS CI 必跑 `--suite core` 的 20 项，不需要 npm/Maven。默认也为 core；`--suite projects` 单独运行 Vite/Maven 两项，`--suite full` 运行全部 22 项，缺少项目依赖会失败。Docker 采集仍需单独验收，macOS ps/lsof 采集不能直接加入 Linux Docker job。

真实桌面验收另行进行：正常安装并逐项审核两个 User config handler，在用户授权的新聊天里让 Codex 运行 `tests/desktop_capture_fixture.py --seconds 120`。夹具直接产生两个 HTTP 服务和一个持续任务，测试的是原始 shell 启动采集，不经 csm launch；普通开发长期服务仍遵循项目配套入口。说明自动附加 arm 检查点及本机元数据副作用，正常申请测试端口/进程权限。夹具清理自己的子进程；核对实际事件、项目、原生窗口运行数量、观察标识和禁用控制，不把手动终端执行当作工具事件。

`check_capture_hooks.py` 只启动独立短时 app-server 检查配置；它不重载桌面共享运行时，ready=true 不能证明旧聊天生效。G-08 必须在安装前已打开的同一旧聊天验证真实事件，G-09 必须验证无 hook 的实际候选及归属排除，均未完成。原生恢复、实际 Quit 后采集器存活和菜单栏/窗口关闭的覆盖边界见 [自动观察报告](results/GLOBAL_CAPTURE.md)。

## 状态界面复验（开发分支）

重新构建后，在解锁桌面使用独立测试状态目录。通过 scripts/csm 登记测试服务及验收窗口，使 GUI、CLI 和观察器使用相同的 `--root`；不使用业务服务制造状态或验证启停。

1. 准备两个测试项目：一项持续运行进程、一项正常退出进程、一项成功任务和一项失败进程。等待实际结束后核对运行状态，不能在刚启动的快照里假定任务已经结束。
2. 检查列表分类、详情和历史中的原始状态，以及选中行可读性。运行中且健康异常的服务应带需关注标记，详情应单独标注健康。
3. 分别选择全部、运行中、未运行、处理中和需关注，切换项目及搜索词，核对列表、数量、排序和空结果。项目汇总独立于筛选。
4. 在另一个项目登记唯一持续运行的测试服务，通过 UI/CLI 停止后，确认项目自动由绿色运行中变为红色已停止，另一项目状态保持不变。
5. 通过登记 ID 停止测试服务和窗口，核对出生身份及测试观察器归属后清理；保留正式组件。截图和运行元数据仅保留本机。

本轮构建、签名和实际窗口结果见 [体验优化验收](results/UX_0_0_3.md)。CI 构建不能替代选中行、真实筛选和项目状态刷新的桌面证据；菜单栏与深色模式的未实测边界另行记录。

## 空闲资源回归

- **PERF-1** 后台无跟踪任务或近期 Docker 事件时，每 5 秒轮询一次，并由采集管道即时唤醒。活动跟踪保留原检查间隔。事件文件原子替换后触发缓存重读，进程关联有变化时立即保存。用核心采集套件核对启动检查点、进程重新挂接和恢复。
- **PERF-2** 自动快照批量读取暂停服务的缓存。窗口可见时保持 2 秒刷新，关闭或隐藏后约每 10 秒更新菜单栏，重开即请求刷新。用扫描策略回归核对缓存、显式刷新范围与并发暂停。
- **PERF-3** 对同一安装和数据分别采样窗口打开、窗口关闭以及优化前后的 CPU 和磁盘 I/O。统计 CPU 时合并 Manager 回收的短时子进程，内存记录常驻进程的实际占用，磁盘写入记录操作系统归因值。

本机复验通过 65 项基础回归及 20 项核心采集回归。224 条服务记录的隔离数据集包含 196 条暂停服务，单次快照 RPC 的中位数由 540 毫秒降至 166 毫秒。新版空闲实测包含 260 条服务记录，每种状态采样约 22 至 24 秒。窗口打开、关闭后菜单栏驻留、仅后台组件运行时，CPU 分别为单核的 23.7%、9.2%、7.8%。各区间系统归因的写入均约 8 KiB。旧版在线采样时的记录数和采样方式不同，以隔离数据集的快照结果作为同负载对照。

## 真实 Vite / Spring / Python 项目

Vite 7.3.7 要求 Node `^20.19.0 || >=22.12.0`（见锁文件）；Java 17+、Maven 3.6.3+、npm 必须可用。可按自己的包管理器安装 Node/Maven/JDK，设置 JAVA_HOME；`/usr/bin/python3 scripts/doctor.py --projects` 检查可执行文件。系统 PATH 优先，也可指定 `CSM_NPM`、`CSM_MAVEN` 的可执行文件绝对路径（空格无需改写源码）。

```sh
/usr/bin/python3 scripts/prepare_fixtures.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_projects.py
```

准备入口运行锁定的 npm ci 和 Maven package，不启动服务；初次 Maven 启动可能继续下载插件依赖。项目测试通过 scripts/csm 登记，默认新建独立状态目录，停止并释放自己的监听端口。`--root <测试目录>` 与 `--keep` 仅用于明确需要保留测试服务的桌面验收，禁止指向正式状态目录。

## Docker 独立复验

```sh
docker pull python:3.12-slim
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_docker.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_docker_failures.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_project_docker.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_capture_docker.py
```

Docker/Compose 可用，测试以唯一标签隔离对象并核对原有容器状态，删除仅测试拥有的容器/卷。镜像版本标签可能更新，结果须同时保留本机具体镜像 ID。

采集验收在同一状态库先手动登记一个普通容器，再通过合成 shell hook 输入采集另一个普通容器及 Compose 成员。它验证真实容器身份和去重，桌面真实回调另行验收。无需 Docker 的同库回归随 PR 的观察策略测试运行。采集夹具可以用 `CSM_CAPTURE_FIXTURE_IMAGE` 选择已缓存且提供 `/bin/sh` 和 `sleep` 的镜像，容器只运行休眠任务。

`.github/workflows/ci.yml` 在 main 推送、PR 或手动触发时运行 macOS 构建、签名、隐私扫描和版本检查，并执行本文的基础回归及项目栏、扫描策略和服务配置测试。核心采集使用 `--suite core`（20 项）。独立 Linux job 检查锁定 npm 夹具依赖的高危公告。仅推送开发分支不会触发此工作流。`.github/workflows/docker.yml` 在独立 Linux runner 上手动验证 Docker/Compose、容器失败场景和容器项目的改名、删除及恢复。应用构建目标为 macOS。工作流只读访问仓库，原始证据留在运行环境。

真实主窗口、菜单栏与实际 Codex 退出继续使用 [验收索引](../specs/001-service-manager/acceptance.md) 在解锁桌面操作；引擎中的合成策略测试只能验证规则，不是实际退出证据。

服务生命周期变更的 [验收记录](results/SERVICE_LIFECYCLE.md) 包含同库 Docker 采集、故障注入、服务回收及原生窗口检查。运行 `./scripts/build.sh` 后，可用 `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_lifecycle_native.py` 复验一次性 macOS 应用的窗口关闭、退出策略、心跳和结果持久化。

## 快照性能基线

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 scripts/benchmark_snapshot_modes.py --image python:3.12-slim --output .test-state/snapshot-samples.json
```

同负载对照使用真实 macOS 窗口、退出观察器和采集器。260 条登记固定为 196 条暂停且已结束、36 条开启且已结束、19 个受管休眠任务、1 个观察任务、4 个带回环 HTTP 检查的受管任务、4 个 Docker 容器（其中 1 个观察对象）。默认每次预热 8 秒，分别采样窗口、菜单栏及仅后台 24 秒，重建夹具重复 3 次。容器使用支持 `/bin/sh` 和 `sleep` 的本机缓存镜像，禁止拉取和端口发布。所有对象在独立状态目录，仅清理夹具的进程及唯一标签容器。

原始记录包含界面应用快照耗时、三类状态变化的可见延迟，以及管理器、观察器、采集器和子进程的计数。CPU 汇总常驻管理进程及其已回收子进程，写入按 macOS 进程归因，合并短命 Python 的退出计数。测试专用的窗口切换和计数记录对两版本相同。业务夹具与 Docker 守护进程自身的资源不计入管理器开销。

窗口可见时，以观察进程退出 8 秒、容器状态变化 14 秒、HTTP 健康变化 11 秒作为这组夹具的可见延迟预算。它们覆盖按类型调度、探测耗时和界面下一次取数。旧版用同一流程采集后再构建新版。新版可追加 `--verify-native`，在采样结束后检查长连接进程复用、重开窗口、断线提示与恢复、手动刷新及日志选择。

本次三轮前后对照见 [快照与后台探测验收](results/SNAPSHOT_PROBING.md)。

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 scripts/benchmark_snapshot.py --counts 5 20 50 --samples 3
```

入口在 .test-state 下创建唯一状态目录，启动真实受监督的休眠任务；不监听端口、不启动 watcher/collector、不访问正式数据库。需要正常进程权限，保留已启用的采集检查点。退出时仅按已确认身份停止测试任务；清理失败保留原路径，便于正常恢复。请在其他测试结束后运行以减少负载干扰。

每个数量场景分别测量引擎 snapshot、Python CPU、快照查询子进程 CPU/次数，以及真实一次性 JSON RPC 的端到端耗时。另列 instrumentation 进程总耗时以区分测量包装开销。CPU 不含常驻监督器/采集器的后台总消耗，纯任务基线也不覆盖健康 URL、容器、未知身份或复杂观察树。原始 JSON 仅留本机；脱敏摘要见 [修复验收](results/REVIEW_HARDENING.md)。
