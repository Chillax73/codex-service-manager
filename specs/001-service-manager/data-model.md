# 数据模型

状态库使用 `manager.sqlite` 和 SQLite WAL。状态目录权限为 0700，私有文件权限为 0600。当前 `PRAGMA user_version` 为 1。首次打开版本 0 时，迁移在 `schema.lock` 保护下事务执行，回填有留存证据的登记时间，失败时回滚。

| 表 | 主键与关系 | 字段 |
|---|---|---|
| projects | id，cwd 唯一 | name、manual、codex_exit、auto_scan、observation_revision、created_at、created_at_source、pinned |
| services | id，project_id → projects | name、kind、spec JSON、keep、group_id、display_name、created_at、created_at_source、pending_spec JSON、config_revision |
| runs | id；service_id → services | started、ended、state、exit_code、runtime JSON、error、log |
| operations | id；target 对应服务 ID | action、trigger、started、ended、result、error |
| service_observations | service_id → services | checked_at、projection JSON，保存最近一次实际检查的公开状态投影 |
| metadata | key | value JSON，保存组件身份与心跳、采集跟踪和诊断、项目栏排序偏好，以及项目与服务的回收记录 |

项目和服务的 `created_at_source` 区分直接登记（`registered`）、按历史估算（`estimated`）和未知（`unknown`）。项目扫描策略实际变化时递增 `observation_revision`，用来拒绝过期的自动观察结果。暂停期间，快照批量读取登记和上次检查的投影。显式刷新仍执行实际检查。

服务显示名称保存在 `display_name`，保存后即时生效。待生效启动与健康配置放在 `pending_spec`，成功启动后并入生效配置。界面保存时携带 `config_revision`，防止旧表单覆盖更新。快照提供待生效摘要和环境键名。

应用服务 ID 默认由规范化 cwd + 稳定名称产生；容器使用完整容器 ID 派生；Compose 由 cwd/file/group/member 派生。显式服务 ID 只接受有限字符集合。run ID 与 operation ID 为独立 UUID。

经配套入口管理的进程 runtime 保存监督 PID/出生时间、子进程 PID/出生时间、进程组。控制请求只向验证身份与 UUID token 的监督器发信号；监督器持有子进程对象并处理整个进程组。

容器 runtime 保存完整 IDs、StartedAt、原生状态、健康状态和实际端口绑定。并发状态查询用服务专属锁串行登记容器运行历史。提前退出也保存可获取的最后一次运行；起止时间优先来自 Docker StartedAt/FinishedAt，启动请求被拒绝时记录失败尝试与诊断。已匹配发布端口的本机健康 URL 保存内部 TCP 端口，快照使用当前宿主端口，保留原始配置。

运行状态和健康检查各自展示；unknown 不代表 stopped。后台任务的成功/失败/取消独立于长期服务自然退出。历史清理仅作用于超过保留期限且 ended 不为空的记录，并保留每个服务的最近运行；期限接受 1–36500 的整数天数，不清除活动运行。日志仅在 ROOT/logs 下与 run ID 对应的普通文件才可读取/删除，拒绝外部路径及符号链接目标；回收档案和其中引用的日志保留。

## 项目管理与恢复

重命名只更新 projects.name，cwd/身份不变。删除登记前需确认没有活动、停止失败、未知或未结束运行；在同一事务把项目/服务/历史/操作复制为 metadata 的 deleted_project:UUID 记录，再移出主表。归档含原本保存的启动配置，仅留私密本机状态库，公开快照只返回摘要。源码、Docker 对象、卷和本机日志不删除。恢复事务拒绝同目录和 ID 冲突，失败不覆盖任何已有记录；恢复不会执行服务启动。回收记录当前保留至恢复，无自动过期或永久删除入口。

单服务删除同样实时核验，再把服务、运行和操作归入 `deleted_service:UUID`。服务与项目删除都移除对应的状态投影。项目恢复回填登记、运行与操作，状态投影在后续实际检查时生成。当前恢复入口针对项目归档。

## 用户级自动观察

复用 services/runs，不新增表。观察对象的 spec 保存 `management=observed`、`source=global_hook`、cwd、session_id/tool_use_id、association 依据和实际 ports；本机对象的 runtime 保存 pid/birth/ports。容器对象还保存 container_id，运行由实际 Docker 状态核对。启动事件不保存整段工具命令、完整环境或工具输出；常见简单命令可保存经保守解析的 launch_recipe.argv 及 launch_env（仅 PATH/LANG/JAVA_HOME）。实际启动器与单项范围验证后，services.spec 保存 argv/env、recipe_automatic；显式配置保存 recipe_explicit。

进程服务 ID 从启动项目、可执行名称、实际 cwd 和端口集合派生；同一候选有另一活动 PID 时增加 PID 区分。ID 不代表已具备控制能力，也不保证未知复杂脚本重启后能关联同一服务。相同已验证身份更新当前运行；身份变化或旧运行结束后产生新 run。端口/cwd 变化的稳定归并尚无一般保证，不能用它认领无关对象。

项目归属优先保留采集事件的启动 cwd；运行 cwd 可不同，不以共享临时目录替代权威启动项目。容器服务 ID 从完整容器 ID 派生；标签关联仅用于有启动上下文的 Compose 对象，已管理容器/成员去重。容器被重新创建有新 ID，不承诺合并为原观察服务。

快照返回 management（managed/attached/observed）、独立 can_start/can_stop/can_restart/can_force、capability_reason、automatic_end 与 can_logs。原观察运行按可靠身份/单项范围和启动配置分别开放能力；本机 can_logs 仍为 false，容器日志可读取。观察 spec.management 保持 observed，因此原运行不参与结束策略；经管理器启动新运行后改为 managed。进程消失时 ended 为观察时间、exit_code 保持未知，可保存 exit_observation 提示。健康与运行状态继续分离，未配置健康检查不根据端口推断可用。

内部任务 `csm-auto-collector` 使用 `source=internal`，常规服务列表、仅含内部任务的项目及批量控制均跳过它。metadata 的 capture 保存 pid/birth/updated/error/tracked/event_count。采集器约每 5 秒更新心跳，错误变化时提前更新。快照核对身份和心跳是否在最近 20 秒内，以计算 alive。`capture_tracking` 保存 PID、出生身份及事件关联，在关联变化时写入，供采集器恢复。

状态目录的 capture-events/*.json 仅保存白名单事件字段。临时私密目录的 FIFO/ack 传递事件 ID、实际父 shell PID 及三个必要环境键，shell 检查点不直接读数据库或进程表。新事件按聊天/工具调用标识生成摘要 ID，相同事件幂等。事件默认不自动过期；显式清理按期限删除已过期事件，但保留 capture_tracking 引用的上下文，不清空跟踪元数据。删除数据库不停止组件或业务服务。

尚未实现的后台发现兜底需要独立来源与归属字段语义，不能伪造 `global_hook` 事件；已退出候选也不能创建虚假的活动运行。后续升级管理能力需单独补齐身份与启动配置，不能由观察 spec 推导完整环境或历史。

接管运行 runtime.launch_members 保存已确认启动器/子进程出生身份，防止监听者退出但启动器存活时启动副本；control_members 保存已发送停止的范围，以支持停止失败后核对剩余对象。不保存 ps 的完整命令参数。控制逐个复核身份，不使用观察进程的共享进程组。新监督运行沿用 service ID、新建 run ID，停止前运行的日志/退出码仍未知。

## 采集策略与诊断（2026-10-27）

无需新增表：metadata.capture_disabled_cwds 保存规范化绝对 cwd 数组，精确匹配后续 hook；不递归，不改变既有服务或项目结束策略。snapshot.projects[].capture_enabled 根据当前目录策略计算。

metadata.capture_diagnostic 保存最近故障的 stage/type/at，经同名快照字段输出；不保存命令、工具输出或异常正文。它与采集器心跳独立，最近故障不表示当前仍故障，心跳正常也不能证明每个聊天回调已执行。
