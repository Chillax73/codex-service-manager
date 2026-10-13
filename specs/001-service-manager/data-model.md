# 数据模型

数据库：状态目录 `manager.sqlite`，SQLite WAL。目录 0700，文件由进程 umask 0077 创建。当前初版 schema 自动创建且索引可重复执行；未来变更应增加版本化迁移，不删除已有数据。

| 表 | 主键与关系 | 字段 |
|---|---|---|
| projects | id；cwd 唯一 | name、manual、codex_exit |
| services | id；project_id → projects | name、kind、spec JSON、keep、group_id |
| runs | id；service_id → services | started、ended、state、exit_code、runtime JSON、error、log |
| operations | id；target 对应服务 ID | action、trigger、started、ended、result、error |
| metadata | key | value JSON，保存退出观察器、自动采集器身份/心跳、跟踪状态与项目回收记录 |

应用服务 ID 默认由规范化 cwd + 稳定名称产生；容器使用完整容器 ID 派生；Compose 由 cwd/file/group/member 派生。显式服务 ID 只接受有限字符集合。run ID 与 operation ID 为独立 UUID。

经配套入口管理的进程 runtime 保存监督 PID/出生时间、子进程 PID/出生时间、进程组。控制请求只向验证身份与 UUID token 的监督器发信号；监督器持有子进程对象并处理整个进程组。

容器 runtime 保存完整 IDs、StartedAt、原生状态、健康状态和实际端口绑定。并发状态查询用服务专属锁串行登记容器运行历史。提前退出也保存可获取的最后一次运行；起止时间优先来自 Docker StartedAt/FinishedAt，启动请求被拒绝时记录失败尝试与诊断。已匹配发布端口的本机健康 URL 保存内部 TCP 端口，快照使用当前宿主端口，保留原始配置。

运行状态和健康检查各自展示；unknown 不代表 stopped。后台任务的成功/失败/取消独立于长期服务自然退出。历史清理仅作用于 ended 不为空的记录，不清除活运行。

## 项目管理与恢复

重命名只更新 projects.name，cwd/身份不变。删除登记前需确认没有活动、停止失败、未知或未结束运行；在同一事务把项目/服务/历史/操作复制为 metadata 的 deleted_project:UUID 记录，再移出主表。归档含原本保存的启动配置，仅留私密本机状态库，公开快照只返回摘要。源码、Docker 对象、卷和本机日志不删除。恢复事务拒绝同目录和 ID 冲突，失败不覆盖任何已有记录；恢复不会执行服务启动。回收记录当前保留至恢复，无自动过期或永久删除入口。

## 用户级自动观察

复用 services/runs，不新增表。观察对象的 spec 保存 `management=observed`、`source=global_hook`、cwd、session_id/tool_use_id、association 依据和实际 ports；本机对象的 runtime 保存 pid/birth/ports。容器对象还保存 container_id，运行由实际 Docker 状态核对。启动事件不保存整段工具命令、完整环境或工具输出；常见简单命令可保存经保守解析的 launch_recipe.argv 及 launch_env（仅 PATH/LANG/JAVA_HOME）。实际启动器与单项范围验证后，services.spec 保存 argv/env、recipe_automatic；显式配置保存 recipe_explicit。

进程服务 ID 从启动项目、可执行名称、实际 cwd 和端口集合派生；同一候选有另一活动 PID 时增加 PID 区分。ID 不代表已具备控制能力，也不保证未知复杂脚本重启后能关联同一服务。相同已验证身份更新当前运行；身份变化或旧运行结束后产生新 run。端口/cwd 变化的稳定归并尚无一般保证，不能用它认领无关对象。

项目归属优先保留采集事件的启动 cwd；运行 cwd 可不同，不以共享临时目录替代权威启动项目。容器服务 ID 从完整容器 ID 派生；标签关联仅用于有启动上下文的 Compose 对象，已管理容器/成员去重。容器被重新创建有新 ID，不承诺合并为原观察服务。

快照返回 management（managed/attached/observed）、独立 can_start/can_stop/can_restart/can_force、capability_reason、automatic_end 与 can_logs。原观察运行按可靠身份/单项范围和启动配置分别开放能力；本机 can_logs 仍为 false，容器日志可读取。观察 spec.management 保持 observed，因此原运行不参与结束策略；经管理器启动新运行后改为 managed。进程消失时 ended 为观察时间、exit_code 保持未知，可保存 exit_observation 提示。健康与运行状态继续分离，未配置健康检查不根据端口推断可用。

内部任务 `csm-auto-collector` 使用 `source=internal`，从常规服务列表与仅含内部任务的项目隐藏；批量控制跳过。metadata 的 capture 保存 pid/birth/updated/error/tracked/event_count，快照按身份及 20 秒心跳计算 alive；capture_tracking 保存已确认 PID/出生身份/事件关联，用于采集器恢复。

状态目录的 capture-events/*.json 仅保存白名单事件字段。临时私密目录的 FIFO/ack 传递事件 ID、实际父 shell PID 及三个必要环境键，shell 检查点不直接读数据库或进程表。新事件按聊天/工具调用标识生成摘要 ID，相同事件幂等。事件文件与跟踪元数据当前没有自动保留期清理；删除数据库不停止组件或业务服务。

尚未实现的后台发现兜底需要独立来源与归属字段语义，不能伪造 `global_hook` 事件；已退出候选也不能创建虚假的活动运行。后续升级管理能力需单独补齐身份与启动配置，不能由观察 spec 推导完整环境或历史。

接管运行 runtime.launch_members 保存已确认启动器/子进程出生身份，防止监听者退出但启动器存活时启动副本；control_members 保存已发送停止的范围，以支持停止失败后核对剩余对象。不保存 ps 的完整命令参数。控制逐个复核身份，不使用观察进程的共享进程组。新监督运行沿用 service ID、新建 run ID，停止前运行的日志/退出码仍未知。
