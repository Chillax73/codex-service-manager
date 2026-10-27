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

## 用户级自动观察（开发分支）

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_capture.py --suite core
# 完整项目套件需先按下文准备 npm/Maven 夹具：
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_capture.py --suite full
# Docker/Compose 可用且 python:3.12-slim 已存在时：
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_capture_docker.py
```

进程入口使用独立临时状态，启动真实采集器及测试进程，需要本机进程/端口权限。现含 21 组：原有采集、普通接管/配置、Vite、离线 Maven/Spring、多服务/已登记任务隔离、身份/超时/防副本及独立 RPC 状态目录。容器入口使用唯一测试标签、已有镜像，比较原有容器状态，现含 7 项检查。hook 输入为显式模拟，不能当作真实桌面回调。原生 UI 的实际操作及独立进程核验见 adoption-ui-tests.json，综合结果见 [普通启动接管](results/ORDINARY_ADOPTION.md)。macOS CI 必跑 `--suite core` 的 19 项，不需要 npm/Maven。默认也为 core；`--suite projects` 单独运行 Vite/Maven 两项，`--suite full` 运行全部 21 项，缺少项目依赖会失败。Docker 采集仍需单独验收，macOS ps/lsof 采集不能直接加入 Linux Docker job。

真实桌面验收另行进行：正常安装并逐项审核两个 User config handler，在用户授权的新聊天里让 Codex 运行 `tests/desktop_capture_fixture.py --seconds 120`。夹具直接产生两个 HTTP 服务和一个持续任务，测试的是原始 shell 启动采集，不经 csm launch；普通开发长期服务仍遵循项目配套入口。说明自动附加 arm 检查点及本机元数据副作用，正常申请测试端口/进程权限。夹具清理自己的子进程；核对实际事件、项目、原生窗口运行数量、观察标识和禁用控制，不把手动终端执行当作工具事件。

`check_capture_hooks.py` 只启动独立短时 app-server 检查配置；它不重载桌面共享运行时，ready=true 不能证明旧聊天生效。G-08 必须在安装前已打开的同一旧聊天验证真实事件，G-09 必须验证无 hook 的实际候选及归属排除，均未完成。原生恢复、实际 Quit 后采集器存活和菜单栏/窗口关闭的覆盖边界见 [自动观察报告](results/GLOBAL_CAPTURE.md)。

## 状态界面复验（开发分支）

重新构建后，在解锁桌面使用独立测试状态目录。通过 scripts/csm 登记测试服务及验收窗口，使 GUI、CLI 和观察器使用相同的 `--root`；不使用业务服务制造状态或验证启停。

1. 准备两个测试项目：一项持续运行进程、一项正常退出进程、一项成功任务和一项失败进程。等待实际结束后核对运行状态，不能在刚启动的快照里假定任务已经结束。
2. 检查列表、详情、历史中的颜色/图标/文字，以及选中行可读性；健康应单独标注。
3. 分别选择运行中、已退出、已停止和需关注；切换项目、输入及清空搜索词，核对列表、数量和空结果。状态逐项精确匹配，项目汇总不受筛选影响。
4. 在另一个项目登记唯一持续运行的测试服务，通过 UI/CLI 停止后，确认项目自动由绿色运行中变为红色已停止，另一项目状态保持不变。
5. 通过登记 ID 停止测试服务和窗口，核对出生身份及测试观察器归属后清理；保留正式组件。截图和运行元数据仅保留本机。

构建、签名和实际窗口已有本机结果，见 [状态显示验收](results/STATUS_DISPLAY.md)。CI 构建不能替代选中行、真实筛选和项目状态刷新的桌面证据；菜单栏与深色模式的未实测边界另行记录。

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
```

Docker/Compose 可用，测试以唯一标签隔离对象并核对原有容器状态，删除仅测试拥有的容器/卷。镜像版本标签可能更新，结果须同时保留本机具体镜像 ID。

`.github/workflows/ci.yml` 在 main 推送、PR 或手动触发时运行 macOS 构建、签名验证、14 组真实进程、9 组端点、3 组目录/工具契约、13 组项目管理契约、隐私扫描、19 组核心采集、5 组 shell/策略契约（含 Bash/zsh 边界矩阵）、7 组存储/清理契约和版本一致性检查，并在独立 Linux job 检查锁定 npm 夹具依赖的高危公告。仅推送开发分支不会自动触发此工作流。`.github/workflows/docker.yml` 在独立 Linux runner 手动执行 Docker/Compose 、真实失败场景及项目改名/删除/恢复。这不表示应用支持 Linux。工作流无写权限、不上传原始证据。

真实主窗口、菜单栏与实际 Codex 退出继续使用 [验收索引](../specs/001-service-manager/acceptance.md) 在解锁桌面操作；引擎中的合成策略测试只能验证规则，不是实际退出证据。

## 快照性能基线

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 scripts/benchmark_snapshot.py --counts 5 20 50 --samples 3
```

入口在 .test-state 下创建唯一状态目录，启动真实受监督的休眠任务；不监听端口、不启动 watcher/collector、不访问正式数据库。需要正常进程权限，保留已启用的采集检查点。退出时仅按已确认身份停止测试任务；清理失败保留原路径，便于正常恢复。请在其他测试结束后运行以减少负载干扰。

每个数量场景分别测量引擎 snapshot、Python CPU、快照查询子进程 CPU/次数，以及真实一次性 JSON RPC 的端到端耗时。另列 instrumentation 进程总耗时以区分测量包装开销。CPU 不含常驻监督器/采集器的后台总消耗，纯任务基线也不覆盖健康 URL、容器、未知身份或复杂观察树。原始 JSON 仅留本机；脱敏摘要见 [修复验收](results/REVIEW_HARDENING.md)。
