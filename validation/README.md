# 第一轮验证

这些脚本使用真实的本机进程和 Docker 容器，验证管理机制；不会调用模型。它们没有实现 Mac 界面或完整 Codex 插件。

实验 hooks 的安装、审核、复验和移除见 [Hooks 事件实验](HOOKS.md)。正式产品的接入方式见 [Codex 桌面接入](../docs/INTEGRATION.md)。

## 文件

- `manager.py`：独立监督进程、SQLite 运行记录、HTTP 检查、停止/重启、结束策略。
- `verify.py`：Python/Node/Java、任务、子进程、失败、恢复及项目策略验证。
- `cross_boundary.py`：跨两个独立 Codex shell 工具调用验证服务继续运行。
- `verify_docker.py`：单容器及 Compose 实验；使用已有的 `python:3.12-slim` 镜像。
- `hook_recorder.py`：仅记录事件名称、聊天 ID、轮次 ID、工作目录、工具名称和时间。只对含 `CSM_POC_MARKER` 的验证命令记录命令文本；不读取聊天正文、不收集环境变量、不自动停止服务。
- `install_hooks.py` 按当前克隆路径生成 `validation/hooks.json` 和项目 `.codex/hooks.json`。遇到不同的已有配置会拒绝覆盖，生成的文件仅留本机。
- `verify_hooks.py`：调用本机 Codex 的 `initialize`、`hooks/list`、`config/read`；不绕过 hooks 信任。仅保存配置层名称和禁用原因，丢弃配置内容。

## 重复运行

在项目根目录运行，需要允许创建本机监听端口、观察/停止本次测试进程。Docker 实验另需启动 Docker Desktop。

```sh
python3 validation/verify.py
CSM_POC_MARKER=round1 python3 validation/cross_boundary.py start
CSM_POC_MARKER=round1 python3 validation/cross_boundary.py finish
python3 validation/verify_docker.py
```

`cross_boundary.py start` 与 `finish` 必须分成两个独立工具调用，才能验证启动工具调用结束后的存活。它们显式通过管理入口启动服务，不代表 hooks 已经实现自动发现。

`.state/` 保存监督进程日志、SQLite 和事件数据。每次进程实验使用独立目录；实验完成后清理服务，保留证据。Docker 实验仅删除其新建并带唯一标记的测试资源。退出 Codex 的策略输入目前是模拟事件，不代表真实退出检测通过。

## 历史 Hooks 验收

首次探测时项目层被禁用，临时 hooks 显示 `untrusted`。随后按用户授权，通过 Codex 正常审核界面信任当前项目与这 5 个项目 hooks，已核对全部 `trusted`、`enabled`，项目层已加载。CLI 测试会话实际写入两次 `SessionEnd`。当时尚未确认桌面会话的工具事件。复验方法见 [Hooks 事件实验](HOOKS.md#配置探测与实际事件复验)。

## 限制

- Node/Java 测试是最小 HTTP 程序，没有验证 Vite、Spring Boot、npm/Maven 启动链或实际项目配置。
- 恢复测试是新管理进程恢复 SQLite 配置和状态，不代表完整 Mac 应用重启、系统重启或监督进程崩溃恢复通过。
- 重启保存 argv、cwd 和显式环境配置；原型只额外保留 PATH、LANG、JAVA_HOME，不保证重放任意用户 shell 的环境与凭据。
- Docker 目前通过独立验证脚本控制，尚未接入统一管理器和运行历史。
- 进程身份保护用启动时间和监督进程标识，不等于生产级进程句柄保证；监督进程异常退出后的接管仍需设计。
- 没有验证安装前的历史恢复、Docker 引擎异常、Mac 睡眠唤醒或真正退出 Codex 后的服务管理。

原始 `*results.json`、日志、本机 hooks 配置仅留本机。正式产品不依赖这些实验 hooks；发布结果见 [脱敏验收报告](../docs/results/VALIDATION.md)。
