# 使用、排障与卸载

## 快速体验

按 README 构建并打开应用后，在自己的测试项目目录通过绝对路径调用 scripts/csm。可以登记一个不监听端口的后台任务：

```sh
/path/to/codex-dev-services-manager/scripts/csm launch --name demo-task --task -- /usr/bin/python3 -c 'import time; print("demo", flush=True); time.sleep(60)'
/path/to/codex-dev-services-manager/scripts/csm list
```

界面可停止/重启并查看每次历史；将返回 service ID 替换到 `csm stop <serviceID>`，通过管理入口停止测试任务。界面关闭不会停止服务。

## 排障

| 现象 | 检查和处理 |
|---|---|
| 缺少 Swift/Python 或构建失败 | 执行 `xcode-select --install`，确认 `/usr/bin/python3 --version` 和 `swiftc --version`；运行 `scripts/doctor.py`。 |
| 项目工具找不到 | npm/mvn 加入 PATH，或设置 CSM_NPM/CSM_MAVEN（可含空格）；Java 17+，必要时设置 JAVA_HOME。安装 Vite 依赖需要支持其 engines 的 Node。 |
| 服务运行但健康不可用 | 核对监听地址、端口和健康路径；查看最小日志，健康和进程存活分别显示。 |
| 端口冲突 | 查明占用者，改用空闲端口；不要直接清理未知进程。 |
| 状态未知或停止失败 | 核对实际进程/容器身份和 Docker 连通性。停止失败不能继续重启；仅对确认拥有的对象使用 UI 显式强制停止。 |
| 容器组件不可用 | 启动 Docker Desktop，检查 `docker info` 和 `docker compose version`，保留原容器和卷。 |
| 退出观察器不可用 | 设置中使用恢复入口；确认目标应用、同一状态目录和实例状态。真实退出与窗口关闭不同。 |
| 找不到之前记录 | 核对 CSM_STATE_DIR/`--root`；GUI、CLI 和观察器应使用同一目录。 |
| 首次 Maven/Node 测试失败 | 先运行 `scripts/prepare_fixtures.py`，检查网络和工具版本；Maven 初次运行仍可能下载插件依赖。 |

## 卸载与数据清理

1. 在 UI/CLI 中逐项停止自己希望结束的服务；持续运行的服务和保留例外需明确决定。`end <projectID>`遵循项目开关，不保证停掉保留例外。不要用旧 PID 直接 kill。
2. 在每个接入项目执行 `/usr/bin/python3 /path/to/codex-dev-services-manager/scripts/integrate.py /path/to/project --remove`。它只移除标记段落，保留其他指令；检查首次备份 AGENTS.md.csm-backup。原型 hooks 如曾安装，检查项目 .codex/hooks.json，将指向 validation/hook_recorder.py 的本工具条目逐项移除，保留其他 hooks；勿删除整个混合配置。
3. 在设置中查看状态目录。退出管理应用后独立退出观察器仍可能存活；用活动监视器检查 ServiceManager 的完整命令及 `--watch --root`，仅退出属于本工具、同一状态目录的观察器。不要按名称批量 kill，不要退出其他状态目录或项目的对象。
4. 确认没有登记服务/观察器在使用数据后，备份需要保留的历史，再通过 Finder 删除 `~/Library/Application Support/Codex Service Manager/`（自定义 CSM_STATE_DIR 以实际配置为准）。删除数据库不会自动停止服务，不要边运行边删除。
5. 删除本地 `.app` 与源码目录。容器、镜像和卷不会随卸载删除；仅在确认归属和数据已备份后用 Docker 的显式命令单独处理。

版本升级重新构建应用；请先备份状态目录。当前无自动数据库迁移或自动更新承诺。
