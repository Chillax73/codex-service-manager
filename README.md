# Codex Service Manager

一个供个人开发使用的原生 macOS 服务管理应用。本项目是独立第三方工具，与 OpenAI 没有官方关联。主要用于管理 Codex 在本机开发时启动的服务：通过配套入口按项目登记前端、后端、后台任务、Docker 容器和 Compose 服务，查看状态、启停、日志与运行历史。

当前版本已完成本机 A-01 至 A-16 验收，包含真实 Codex 退出检测。真实桌面实测环境为 Apple Silicon / macOS 15.7.3；构建目标 macOS 13+。macOS 14/15 的构建与引擎回归已通过 GitHub CI；Intel 和其他系统版本的窗口/Codex 退出流程尚未人工验收。

![真实原生窗口：仅临时测试项目和服务](docs/images/app-preview.png)

截图来自实际应用的独立测试状态，[图片来源与复核](docs/images/README.md)。

## 功能

- 项目归属与搜索：使用规范化工作目录区分项目，支持运行中和需关注筛选。
- 状态：区分进程运行状态、HTTP/容器健康与管理组件不可用。
- 控制：停止、启动已保存服务、重启；任务取消和重新执行。停止失败时禁止继续重启。
- Docker/Compose：单容器、单成员、整组控制；停止保留容器与数据，组成员操作去重。
- 历史：每次运行独立记录起止时间、退出码、诊断及日志；本机日志按运行分离并滚动。
- 项目结束：每个项目独立选择手动结束、退出 Codex、两者或持续运行；服务可设置保留例外。
- 原生界面：主窗口、系统菜单栏、详情、日志、历史、配置、发现与观察器恢复。

## 构建与启动

需要 macOS 13+ 和 Command Line Tools（Swift 与系统 `/usr/bin/python3`）。缺少工具时运行 `xcode-select --install`，安装完成后再继续。产品 Python 后端仅使用标准库，无需 pip 安装。Docker 功能另需 Docker CLI/Desktop 与 Compose；运行 Java/Node 服务需自行安装其运行时。

```sh
git clone https://github.com/Chillax73/codex-service-manager.git
cd codex-service-manager
/usr/bin/python3 scripts/doctor.py
./scripts/build.sh
open "build/Codex Service Manager.app"
```

构建生成本机 ad-hoc 签名的 `.app`；[v0.1.0 开发者预览](https://github.com/Chillax73/codex-service-manager/releases/tag/v0.1.0) 提供源码构建，未提供可下载安装的应用包或公证安装包。关闭主窗口继续管理；退出管理应用保留服务与独立退出观察器。

## 接入项目

在当前克隆的根目录初始化本机项目指令：

```sh
# 已有 AGENTS.md 时保留原文件。
[ -f AGENTS.md ] || cp AGENTS.example.md AGENTS.md
/usr/bin/python3 scripts/integrate.py "$PWD"
```

接入其他项目，把最后一个参数改为该项目目录。在 Codex 中重新打开目标项目并开始新会话，确保加载更新的 AGENTS.md；然后让 Codex“按照本项目 AGENTS.md 启动前端/后端服务并登记到服务管理器”。安装器保留原指令、保存首次备份，并写入当前克隆路径的配套入口；生成的本机指令不提交 Git。

```sh
# 以下命令在目标项目目录执行；把入口路径替换为当前克隆的路径。
/path/to/codex-service-manager/scripts/csm launch --name frontend --url http://127.0.0.1:5173 --port 5173 -- npm run dev
/path/to/codex-service-manager/scripts/csm launch --name backend --url http://127.0.0.1:8080/health --port 8080 -- mvn spring-boot:run
/path/to/codex-service-manager/scripts/csm launch --name worker --task -- python3 worker.py
```

服务脱离启动工具进程存活，管理界面显示其记录。已有容器在创建后通过 `csm container` 登记；已有 Compose 项目使用原项目名登记。完整命令见 [接入说明](docs/INTEGRATION.md)。未经配套入口启动的普通进程只发现，不承诺自动接管或重启。

## Codex Hooks 的作用与使用

当前正式版使用下列机制管理 Codex 启动的服务：

| 机制 | 职责 |
|---|---|
| `AGENTS.md` + `scripts/csm` | 引导 Codex 遵循项目约定，通过入口启动/登记服务，保存项目归属与每次运行。 |
| 原生退出观察器 | 监听 Codex 应用最后一个实例终止，按项目配置停止服务，保留例外优先。 |
| `validation/` 中的 Codex hooks | 可选的早期实验，仅记录生命周期/工具事件元数据，未接入正式服务自动登记或启停。 |

日常管理按上面的“接入项目”使用即可，无需安装实验 hooks。项目指令是让 Codex 使用登记入口的约定；当前 hooks 不会自动改写普通启动命令或接管所有已有进程。`SessionEnd` 也可能由归档、删除会话或闲置触发，不能作为应用退出的唯一判断。[OpenAI Hooks 官方说明](https://learn.chatgpt.com/docs/hooks#sessionend)

如需复验 hooks，在本仓库克隆根目录进行：

1. 阅读 `validation/install_hooks.py` 和 `validation/hook_recorder.py`，然后生成本机配置：

   ```sh
   /usr/bin/python3 validation/install_hooks.py
   ```

   生成 `.codex/hooks.json` 和 `validation/hooks.json`，配置五个事件：`SessionStart`、`PreToolUse`、`PostToolUse`、`Stop`、`SessionEnd`。已有不同配置时安装器拒绝覆盖，请先处理冲突或使用独立克隆目录试验。

2. 从此目录启动 `codex` CLI，按正常流程信任项目；在 CLI 内输入 `/hooks`，阅读并信任这些 hooks。安装脚本不授予信任；修改定义后需要重新审核。[官方审核说明](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)

3. 可在另一个终端、同一仓库根目录探测配置：

   ```sh
   /usr/bin/python3 validation/verify_hooks.py --project-only
   ```

   它检查配置与信任元数据，不会调用模型，也不证明桌面工具事件已经触发。请核对 `/hooks` 中的本项目配置与启用状态，再让 Codex 执行指南中的无害标记命令，查看本机 `validation/.state/hook-events.jsonl` 的实际事件。

事件文件包含项目路径和会话标识；仅留本机，勿提交 Git 或作为原始附件上传。关闭实验时先在 `/hooks` 禁用本工具条目，再仅移除本工具配置，保留其他 hooks。完整事件复验、路径/版本排障与移除步骤见 [Hooks 使用指南](docs/HOOKS.md)；[原型验证记录](validation/ROUND1_REPORT.md) 与正式产品验收分别保存。

## 结束策略与边界

手动结束默认开启，退出 Codex 默认关闭，两个开关独立。关闭 Codex 窗口、切换/归档聊天或回复结束不会触发退出策略；指定桌面应用最后一个实例终止才触发。系统无法区分普通退出和强制退出，两者都触发；系统关机不保证停止完成。

本机服务先正常停止，等待 5 秒，超时需显式强制停止。容器使用 Docker 原生 5 秒 stop，超时由 Docker 终止。远程服务、任意既有进程接管、开机自启不在当前范围内。

## 验收结果

| 验证类别 | 已通过 |
|---|---:|
| 本机进程集成 | 14 组 |
| 真实 Vite / Spring Boot / Python 项目 | 6 项 |
| Docker / Compose 集成 | 15 项 |
| 真实容器失败场景 | 8 项 |
| 端点、时间与日志契约 | 9 组 |
| 实际原生界面 | 28 项 |

实际退出 Codex 后，本机服务、单容器、两个 Compose 成员共四项按策略停止；保留例外和另一项目继续同次运行，观察器独立存活。用户确认关闭窗口不误停、系统菜单栏详情入口可用。菜单栏快捷控制未逐项重复人工点击，明确保留该验证边界。

测试资源已清理，原有容器状态保持不变。结果见 [验收报告](docs/results/VALIDATION.md)、[机器可读脱敏摘要](docs/results/validation-summary.json)及 [A-01 至 A-16 索引](specs/001-service-manager/acceptance.md)。这些是本机验收记录，不是 GitHub CI 或全平台通过声明。

## 复验

基础测试入口自动创建本机目录，无需手动 mkdir：

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_support.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_endpoints.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_engine.py
```

真实项目测试需要 Node（Vite 要求 `^20.19.0 || >=22.12.0`）、npm、Java 17+、Maven 3.6.3+；工具从 PATH 查找，也可配置 CSM_NPM/CSM_MAVEN。运行 `/usr/bin/python3 scripts/prepare_fixtures.py` 安装锁定的 npm/Maven 夹具依赖，再运行 `tests/verify_projects.py`，默认使用独立测试状态。详细安装、Docker 和 CI 步骤见 [复现与 CI](docs/TESTING.md)。

[GitHub CI](https://github.com/Chillax73/codex-service-manager/actions/workflows/ci.yml) 检查 macOS 构建、Python 回归与发布隐私；[Docker 集成](https://github.com/Chillax73/codex-service-manager/actions/workflows/docker.yml) 独立手动执行。CI 不包含实际窗口、系统菜单栏或真实 Codex 退出，仍需按 [验收索引](specs/001-service-manager/acceptance.md) 人工复验。原始证据仅保存在本机 `docs/evidence/`。

## 使用与维护

最小任务体验、故障排查、移除项目接入、停止独立观察器和数据清理见 [使用与卸载](docs/OPERATIONS.md)。删除数据目录不会停止服务，退出管理应用也不会自动停止观察器。

欢迎通过模板提交问题或建议；开发流程见 [贡献说明](CONTRIBUTING.md)，安全问题使用 [私密反馈](SECURITY.md)。版本变化见 [CHANGELOG](CHANGELOG.md)，开源收尾证据见 [发布核验](docs/results/OPEN_SOURCE_READINESS.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方依赖保留其各自许可，来源和分发边界见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。

## 隐私

本仓库从脱敏源码快照独立初始化，不携带原私有仓库的 Git 历史。

运行数据保存在 `~/Library/Application Support/Codex Service Manager/`，可通过 `CSM_STATE_DIR` 隔离。应用仅保存必要环境和显式覆盖，UI 隐藏环境值；不要把凭据写入会公开的命令、健康 URL 或日志。

原始 JSON、截图、日志、数据库、本机 hooks 和带绝对路径的项目指令均排除出 Git。发布摘要按字段白名单生成，只保留通过数量、覆盖范围及能力边界，不包含个人路径、邮箱、聊天标识、进程/容器 ID 或环境值。

```sh
# 已有完整本机验收证据时，重新生成脱敏结果。
/usr/bin/python3 scripts/export_results.py
/usr/bin/python3 scripts/check_publish.py
```

## 项目文档

- [需求文档](docs/PROJECT_REQUIREMENTS.md)
- [Coding spec](specs/001-service-manager/spec.md)、[实现设计](specs/001-service-manager/plan.md)、[数据模型](specs/001-service-manager/data-model.md)
- [实施任务](specs/001-service-manager/tasks.md)、[实施报告](docs/IMPLEMENTATION_REPORT.md)
- [第一轮原型记录](validation/ROUND1_REPORT.md)（独立于产品验收）
