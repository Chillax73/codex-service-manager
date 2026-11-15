# Codex 桌面接入

可安装用户级自动观察，也可通过项目配套入口登记服务并获得完整控制。

## 任意本机聊天自动观察

`--instructions` 会同时给全局 AGENTS.md 添加带标记的正常采集说明，保留原指令并首次备份；用于让其他聊天在申请权限时说明采集写入的范围。它明确禁止绕过审批，不授予额外业务权限。移除时同样带上 `--instructions`，只移除本工具段落。

在本克隆中执行 `/usr/bin/python3 scripts/install_global_capture.py --instructions`，它合并写入 `~/.codex/hooks.json` 的两个本工具 handler，首次修改已有文件时备份，保留其他 hooks。通过 Codex CLI `/hooks` 逐个审核 User config 来源的 `PreToolUse`、`PostToolUse`，命令为本克隆的 `backend/capture.py hook`。命令包含克隆的绝对路径，移动克隆或修改定义后需更新安装并重新审核。安装后开始新聊天加载配置。

在新聊天正常启动服务，检查原生窗口的“自动观察”标识。配置、心跳、旧聊天漏记及恢复方式见 [自动观察排障](OPERATIONS.md#另一个聊天或窗口启动服务却没有记录)。

采集器经 scripts/csm 登记为内部任务，独立于窗口运行。TCP 监听进程分别显示，无端口叶子任务持续约 30 秒后显示。shell 启动前后的检查点通过本机临时 FIFO 唤醒采集器并核对身份，末尾检查点保留原命令退出码。常见 `nohup … &` 返回前先记录其子进程。采集记录只保存白名单元数据和经验证的结构化启动信息。观察对象按能力开放控制，原观察运行排除在自动结束策略之外。

直接 `docker run -d` 返回容器 ID、直接 Compose 启动配置与实际标签匹配时自动登记。复杂 shell 包装、脚本内部 Docker 操作、在采样前已脱离全部已知父进程的启动方式可能漏记；远端和未触发 hooks 的工具路径不覆盖。启用前的已有服务不被事后认领。本机进程日志和退出码未采集，观察到退出的时间可能晚于实际退出。详细验证见 [自动观察报告](results/GLOBAL_CAPTURE.md)。

### 命令兼容范围

Bash/zsh 回归比较了原命令和带检查点命令的 stdout、stderr 与退出码，覆盖多行命令、heredoc、管道与 pipefail、set -e、EXIT/TERM trap、显式 exit/exec 和超时终止。显式 exit/exec、errexit 和信号可能跳过末尾检查点。依赖 shell 初始状态、DEBUG trap、xtrace 或其他 Hook 改写顺序的命令需要单独实测。采集标记用于避免重复插入，原工具输入的其他字段保留。复验见 [采集回归](TESTING.md#用户级自动观察开发分支)。

申请启动服务所需权限时，说明 shell 检查点及必要的本机元数据写入，保留正常审核流程和现有授权范围。项目级禁用与全局移除分别见 [采集开关](OPERATIONS.md#项目采集开关与历史保留) 和 [卸载说明](OPERATIONS.md#卸载与数据清理)。

## 普通启动后接管（开发分支）

已观察服务可按可靠身份及启动关联停止。支持逐个核对启动器/子进程，不停止启动 shell 或共享进程组；共享多服务启动器、未知子进程、身份变化时显示原因并拒绝操作。端口核验覆盖实际 TCP 监听、IPv4/IPv6。

直接 Python HTTP、Node 脚本、npm dev/start、npx vite、Java jar/classpath 和 Maven spring-boot:run 的简单命令可保存结构化 argv。含重定向、后台符、管道、展开、环境赋值或敏感参数的整段命令不自动重放；只读取 PATH/LANG/JAVA_HOME，不复制完整环境。自动候选必须匹配实际单项启动器。实际 Vite 与 Maven/Spring 启动链已复验。

历史对象、nohup 和复杂脚本缺少配置时，可从服务行右键或详情菜单打开“编辑服务配置…”，填写仅启动该项服务的命令、实际 cwd 和必要环境。已有环境默认保留，界面只显示键名，可按键更新或删除。启动配置在下次成功启动时生效，重启时先停止旧运行。Docker 与 Compose 的配置编辑支持名称和健康地址，启动参数由原容器或 Compose 文件管理。也可使用以下命令。

```sh
/绝对路径/codex-service-manager/scripts/csm configure-observed SERVICE_ID --cwd /项目目录 --env KEY=VALUE -- 原启动命令及参数
```

重启先核对并停止旧树、确认端口释放，再使用管理器监督器创建新运行，沿用 service ID。停止失败不能继续重启。原运行仍排除在项目结束策略之外；经管理器再次启动后升级为已管理。日志从新运行开始采集。多服务脚本不会被猜测成单项重启配置，需拆分；特殊 daemon、自我重启/共享启动器不能保证通用自动接管。详见 [验收报告](results/ORDINARY_ADOPTION.md)。

## 项目配套完整控制

配套启动入口不依赖工具 hooks。项目只需接入一次，之后 Codex 按项目指令登记正常服务启动，无需用户逐次填写配置。

```sh
/usr/bin/python3 /绝对路径/codex-service-manager/scripts/integrate.py /项目目录
```

安装器保留目标目录原有的 `AGENTS.md`，加入带标记的段落，首次修改前备份。`--remove` 移除安装器添加的段落。每个克隆或 worktree 按自己的接入状态操作，Codex 新会话加载目标目录的项目指令后生效。本仓库的开发约定见 [贡献指南](../CONTRIBUTING.md#开发文档)。

示例：

```sh
./scripts/csm launch --name frontend --url http://127.0.0.1:5173 --port 5173 -- npm run dev
./scripts/csm launch --name backend --url http://127.0.0.1:8080/health --port 8080 -- mvn spring-boot:run
./scripts/csm launch --name worker --task -- python3 worker.py
./scripts/csm container CONTAINER_ID --cwd /项目目录
./scripts/csm compose --file /项目目录/compose.yaml --cwd /项目目录
```

工作目录自动归属项目；同一项目与稳定名称形成同一服务。显式环境参数 `--env KEY=VALUE` 在命令前填写。需要 shell 管道时明确指定 `/bin/sh -lc '…'`。容器由 Codex 在原 Docker 启动命令返回 ID 后直接调用登记入口，用户不用填写容器信息。

未启用全局自动观察时，未经辅助入口的普通进程仅出现在发现列表，不承诺自动接管和可重启。手动发现接管 Docker 需选择项目；Compose 默认生成隔离项目名，已有项目需提供原 `--project`。

退出观察器由原生应用或配套入口启动，订阅目标 bundle ID 的最后实例终止。当前目标 `com.openai.codex`，可用状态目录的 `watcher-config.json` 修改 `bundle_ids` 后重启观察器。它独立于管理窗口和 Codex 生存，强制退出也产生系统终止通知；关机不保证清理完成。

参考官方文档：[项目指令](https://learn.chatgpt.com/docs/agents-md)、[Skills](https://learn.chatgpt.com/docs/build-skills)。这里采用项目指令接入，尚未发布 Marketplace 插件。

## 早期 Hooks 实验

早期项目级 hooks 用于记录事件并核对 CLI 与桌面实际触发范围。复现步骤和 SessionEnd 边界见 [Hooks 事件实验](../validation/HOOKS.md)。
