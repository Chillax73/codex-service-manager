# 实现设计

## 架构

原生 `.app`（SwiftUI 工作区 + AppKit 菜单栏）→ JSON stdin/stdout RPC → Python 标准库管理引擎 → SQLite WAL。

每个本机运行由独立监督进程持有子进程，脱离启动工具和 UI 生存。监督进程负责信号、输出滚动、自然完成和退出码。管理请求通过文件锁串行写入；读操作独立，GUI 在后台执行不阻塞主线程。

独立原生观察器订阅 NSWorkspace 应用终止，按 bundle ID 与实例集合判断最后实例退出，再调用引擎批量结束。观察器无需模型、无需聊天 Hook；SessionEnd 从不作为退出信号。退出观察器有独立 PID/身份状态和日志。

Docker 适配器使用明确容器 ID、Compose project/service/config_files/working_dir 标签与保存配置核对对象；引擎不可用时保留历史并显示未知。Compose 组展开成员，统一批量操作，每个成员只执行一次。

配套入口 `scripts/csm` 提供 launch / container / compose / list / stop / restart 等命令，cwd 自动成为项目。项目接入安装器加入带边界标记的 AGENTS.md 段落（可移除），要求 Codex 服务启动走该入口。接入文档随项目分发，不要求依赖未证实的 hooks。

## 数据模型

- projects(id, cwd 唯一, name, manual, codex_exit)
- services(id, project_id, name, kind, spec JSON, keep, group_id)
- runs(id, service_id, started, ended, state, exit_code, runtime JSON, error, log)
- operations(id, service_id/project, action, trigger, started, ended, result, error)
- metadata(key, value) 保存观察器状态/事件。

必要环境字段受本机文件权限保护。启动配置的只读展示应隐藏 env 值。

## 接口

`engine.py rpc` 从 stdin 读取单个 JSON `{action, ...}`，stdout 输出 `{ok, result}` 或 `{ok:false,error}`，错误退出非零。主要 action：snapshot、launch、register_container、register_compose、start/stop/restart/force、project_policy、service_keep、end_project、codex_exit、history、logs、discover、cleanup、watcher_event。

所有启动保存 argv 数组，不通过拼接 shell 命令执行；GUI 手工输入命令仅由 shlex 分词，需要 shell 语义时明确使用 shell argv。

## 界面设计

视觉主张：macOS 原生浅深色自适应，蓝色操作强调，紧凑可读的分栏工作区。
内容：左侧项目导航、中间服务/搜索/状态筛选、右侧配置/日志/历史；菜单栏为快速操作。
交互：选择详情过渡、操作状态反馈、系统 sheet 配置。尊重减少动态效果设置。

## 构建与测试

用 `swiftc` 链接 SwiftUI/AppKit，脚本生成 Info.plist 并复制后端到 app Resources，ad-hoc codesign；无需完整 Xcode。Python 单元测试涵盖策略/身份/去重，独立状态目录实际集成测试验证进程树、真实启动链、Docker 和失败情况。GUI 用原生自动化验证实际可操作流程。

实际 Codex 退出由用户执行，独立 `tests/record_codex_quit.py` 经 `scripts/csm --task` 登记后观察原 Codex PID/出生身份消失、正式观察器产生的 codex_exit 操作、四项应停止服务及两项保留服务。记录程序不调用 codex_exit，不控制 Codex 界面。窗口关闭和菜单栏可见性需要实际界面证据或明确标注的人工观察；进程结果不能替代窗口证据。

最终采用自动原生 UI 与人工真实桌面操作共同验收：28 项原生断言、用户确认菜单栏详情/关闭 Codex 窗口、独立实际退出记录。测试结束后按精确 service ID、容器标签、Compose 项目和卷标签清理，测试历史先归档，再移除本轮登记以免留下不存在容器的异常项；保留正式观察器及原有服务记录。

## 开源交付方案

在独立公开快照中实施，不迁移私有 Git 对象。测试 support 模块统一创建 ignored 工作目录并解析工具；真实项目默认使用唯一测试状态。应用继续使用系统 Python，不打包运行时。CI 使用只读权限与固定官方 checkout 提交，不上传原始证据。Docker 用独立临时 runner；真实窗口截图仅测试数据，经人工查看和 SHA-256 白名单后发布。依赖只供测试安装，源码发行包不包含 node_modules、JAR 或系统框架。首版采用 GitHub prerelease 自动生成源码归档，构建安装包/签名/公证留待二进制分发。

## Hooks 文档补充

在 README 展示职责表和最短 hooks 实验流程，docs/HOOKS.md 说明配置文件、既有配置冲突、事件文件、隐私、桌面验证边界、排障和移除；validation/README.md 链接新版入口并保留原型历史。只改说明，不修改正式登记/退出链或自动安装、信任 hooks。

## 项目管理实现

使用现有 projects.name，无 schema 迁移。新增 project_rename、project_delete_preview、project_delete RPC；变更持有全局 mutation_lock，删除重新 observe 归属服务并在单个 SQLite 事务归档至 metadata 回收记录后移出关联表，不执行信号、Docker rm 或磁盘项目清理。只读快照容忍并发删除的服务行消失。CLI 增加 rename-project/delete-project（显式确认）便于诊断。SwiftUI 项目菜单打开改名/删除 sheet；删除 sheet 展示范围、数量、阻碍和重新检查，最终请求仍以后端为准。Store 在刷新后校正选择，并丢弃变更前已发出的旧快照。

复用 metadata 的 deleted_project:UUID 命名空间，无 schema 迁移；快照仅暴露名称/目录/时间/数量摘要。恢复校验目录和标识冲突并事务回填，随后移除归档。界面提供撤销删除和设置中的恢复列表。项目行使用 Equatable 避免刷新使上下文菜单收起，补齐标准编辑菜单。
