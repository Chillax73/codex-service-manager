# Codex 桌面接入

当前使用项目配套启动入口，不依赖工具 hooks 是否触发。项目只需接入一次，之后 Codex 按项目指令自动登记正常服务启动，无需用户逐次填写配置。

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

未经辅助入口的普通进程仅出现在发现列表，不承诺自动接管和可重启。手动发现接管 Docker 需选择项目；Compose 默认生成隔离项目名，已有项目需提供原 `--project`。

退出观察器由原生应用或配套入口启动，订阅目标 bundle ID 的最后实例终止。当前目标 `com.openai.codex`，可用状态目录的 `watcher-config.json` 修改 `bundle_ids` 后重启观察器。它独立于管理窗口和 Codex 生存，强制退出也产生系统终止通知；关机不保证清理完成。

参考官方文档：[项目指令](https://learn.chatgpt.com/docs/agents-md)、[Skills](https://learn.chatgpt.com/docs/build-skills)。这里采用项目指令接入，尚未发布 Marketplace 插件。
