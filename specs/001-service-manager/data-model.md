# 数据模型

数据库：状态目录 `manager.sqlite`，SQLite WAL。目录 0700，文件由进程 umask 0077 创建。当前初版 schema 自动创建且索引可重复执行；未来变更应增加版本化迁移，不删除已有数据。

| 表 | 主键与关系 | 字段 |
|---|---|---|
| projects | id；cwd 唯一 | name、manual、codex_exit |
| services | id；project_id → projects | name、kind、spec JSON、keep、group_id |
| runs | id；service_id → services | started、ended、state、exit_code、runtime JSON、error、log |
| operations | id；target 对应服务 ID | action、trigger、started、ended、result、error |
| metadata | key | value JSON，保存观察器身份与心跳 |

应用服务 ID 默认由规范化 cwd + 稳定名称产生；容器使用完整容器 ID 派生；Compose 由 cwd/file/group/member 派生。显式服务 ID 只接受有限字符集合。run ID 与 operation ID 为独立 UUID。

进程 runtime 保存监督 PID/出生时间、子进程 PID/出生时间、进程组。控制请求只向验证身份与 UUID token 的监督器发信号；监督器持有子进程对象并处理整个进程组。

容器 runtime 保存完整 IDs、StartedAt、原生状态、健康状态和实际端口绑定。并发状态查询用服务专属锁串行登记容器运行历史。提前退出也保存可获取的最后一次运行；起止时间优先来自 Docker StartedAt/FinishedAt，启动请求被拒绝时记录失败尝试与诊断。已匹配发布端口的本机健康 URL 保存内部 TCP 端口，快照使用当前宿主端口，保留原始配置。

运行状态和健康检查各自展示；unknown 不代表 stopped。后台任务的成功/失败/取消独立于长期服务自然退出。历史清理仅作用于 ended 不为空的记录，不清除活运行。
