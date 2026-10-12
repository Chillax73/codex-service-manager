# Codex 桌面接入

开发分支提供用户级自动观察和项目配套完整控制两种接入。

## 任意本机聊天自动观察

`--instructions` 会同时给全局 AGENTS.md 添加带标记的正常采集说明，保留原指令并首次备份；用于让其他聊天在申请权限时说明采集写入的范围。它明确禁止绕过审批，不授予额外业务权限。移除时同样带上 `--instructions`，只移除本工具段落。

在本克隆中执行 `/usr/bin/python3 scripts/install_global_capture.py --instructions`，它合并写入 `~/.codex/hooks.json` 的两个本工具 handler，首次修改已有文件时备份，保留其他 hooks。通过 Codex CLI `/hooks` 逐个审核 User config 来源的 `PreToolUse`、`PostToolUse`，命令为本克隆的 `backend/capture.py hook`；不使用绕过信任选项。安装后开始新聊天，既有聊天未重新加载时不会使用新增配置。

`/usr/bin/python3 scripts/check_capture_hooks.py /项目目录` 启动独立短时 app-server，只读检查 user/trusted/enabled，不请求模型。它不连接桌面共享运行时、不重载旧聊天；配置可用不代表实际回调已经执行。再在新聊天正常启动服务，检查原生窗口的“自动观察”标识。查看“管理设置 → 服务自动采集”的心跳与错误，可用“启动或恢复自动采集”恢复组件。

采集器经 scripts/csm 登记为内部任务，独立于窗口运行。TCP 监听进程分别显示，无端口叶子任务持续 30 秒后显示。shell 启动前后通过本机临时 FIFO 核对身份，末尾检查点保留原命令退出码；常见 `nohup … &` 返回前先记录其子进程。不会记录原始命令、输出、完整环境或聊天正文。观察对象的后端及所有界面控制均受能力限制，不参与自动结束策略。

直接 `docker run -d` 返回容器 ID、直接 Compose 启动配置与实际标签匹配时自动登记。复杂 shell 包装、脚本内部 Docker 操作、在采样前已脱离全部已知父进程的启动方式可能漏记；远端和未触发 hooks 的工具路径不覆盖。启用前的已有服务不被事后认领。本机进程日志和退出码未采集，观察到退出的时间可能晚于实际退出。详细验证见 [自动观察报告](results/GLOBAL_CAPTURE.md)。

旧聊天加载缺口已复现：新开窗口继续安装前的聊天，没有产生采集事件；当前没有已验收的正常热加载步骤。临时使用新聊天，或使用下方配套入口。不要重复启动已运行对象；没有可信身份/归属时保留在发现页。后台自动发现补录和复杂脚本控制仍待实施，见 [任务清单](../specs/001-service-manager/tasks.md)。

本工具 handler 的命令包含克隆绝对路径，移动/删除克隆或更改定义后需更新安装并重新正常审核。启用后 shell 命令附加 arm 检查点，本机服务启动审批应说明必要元数据写入；全局指令说明不等于授予业务操作权限。

移除：先在 `/hooks` 禁用本工具的两个 User config handler，再执行 `/usr/bin/python3 scripts/install_global_capture.py --remove --instructions` 并确认加载过旧配置的聊天不再执行本工具回调。当前没有经过验收的旧聊天热加载入口；只改磁盘配置不能证明旧运行时已更新。需要停止采集组件时执行 `scripts/csm stop csm-auto-collector`。移除不会停止业务服务或删历史，也不移除项目完整控制约定。

## 项目配套完整控制

配套启动入口不依赖工具 hooks。项目只需接入一次，之后 Codex 按项目指令登记正常服务启动，无需用户逐次填写配置。

```sh
/usr/bin/python3 /绝对路径/codex-service-manager/scripts/integrate.py /项目目录
```

安装器保留原 AGENTS.md，加入带标记的段落，首次修改前备份；`--remove` 移除自己的段落。Codex 新会话加载项目指令后生效。本项目已接入。

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

## 可选 Hooks 实验

配套完整控制依赖上述 AGENTS.md 和 scripts/csm；用户级自动观察使用 backend/capture.py。validation/ 早期 hooks 只记录事件，不登记或启停服务。安装、审核、复验与移除见 [Hooks 使用指南](HOOKS.md)，不应把 SessionEnd 直接映射为应用退出。
