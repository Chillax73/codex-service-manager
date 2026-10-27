# 设计

保持单次 stdin/stdout RPC 与 SwiftUI，避免同时改动进程身份模型和通信协议。

- `storage.py` 集中 SQLite 建表和私有目录/原子 JSON；引擎保留调用包装，所有模块共享当前 ROOT。
- `maintenance.py` 处理保留期限与路径白名单；清理保留每服务最近运行、未结束运行及项目回收档案。日志只能是 ROOT/logs 下与 run ID 对应的普通文件，拒绝链接/路径逃逸。
- `capture.py` 按规范化 cwd 查询独立 metadata 策略。禁用只影响后续 hook，保留既有观察与控制。故障只保存阶段、异常类型和时间，shell 自身输出不增加故障正文。
- Swift 拆分 Models、Status、Infrastructure、Store；构建编译所有生产 Swift 源文件，夹具单独排除。
- shell 插桩保留原文；显式 exit/exec、errexit、信号可能跳过最终检查点，这是采集边界。测试验证相同行为，不承诺对任意 shell 状态透明。
- 性能基线使用唯一测试 ROOT 和真实受监督任务，计量 snapshot 的进程查询及 Python CPU；不读写正式数据库，不引入跨请求身份缓存。

测量发现监督器在主进程存活且无停止请求时仍每 0.1 秒扫描完整进程组。改为仅在停止/强制停止请求或主进程退出时查询成员；每次发信号仍执行既有身份和组核验。保留 0.1 秒请求响应，不引入 PID 缓存或常驻 IPC。
