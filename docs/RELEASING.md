# 发布与兼容性检查

当前只提供源码开发者预览。ad-hoc 本机签名通过不等于 Developer ID 签名、公证或独立安装包可分发。

发布时逐项完成并在 Release 说明中记录证据：

- 同步 README 的当前源码版本、CHANGELOG 最新版本、SECURITY 的支持版本和 scripts/build.sh 的版本/构建号；运行 `/usr/bin/python3 scripts/check_release.py`。
- 在干净 checkout 执行 docs/TESTING.md 的核心测试与原生构建，核对打包的全部 Python 模块；记录 commit、macOS、架构、Python 和 Codex 版本。CI 矩阵的 runner 标签不能代替实际版本或芯片信息。
- 完整项目套件单独准备 npm/Maven 依赖并执行；Docker/Compose 套件在隔离对象上复验，保存镜像 ID。原生窗口/菜单、设置滚动、退出观察器和新/旧 Codex 聊天回调分别人工验收，未完成项写明。
- 使用 `scripts/benchmark_snapshot.py --counts 5 20 50 --samples 3` 保存本机基线。区分纯进程、健康检查、Docker/Compose；需要时增加对应场景，不能从纯任务基线推断容器开销。
- 发布前检查 `git diff --cached` 并运行 scripts/check_publish.py。只发布脱敏摘要，不上传测试 ROOT、数据库、原始日志、截图、环境、用户路径或会话标识。
- 如发布应用包，另需维护者提供 Developer ID 证书和公证环境，签名所有嵌套代码，验证 Gatekeeper/公证结果及干净用户环境的首次运行、升级、hooks 迁移和卸载；凭据不得入库。尚未完成这些步骤时只发布源码，不标记为已公证。
- 至少分别记录 Apple Silicon 与 Intel 以及目标 macOS 版本的结果；无相应设备的项保留“未验证”。测试目标 macOS 13+ 不等于所有版本兼容。
- 核对 README、安装/卸载说明、已知限制、[Bug 模板](../.github/ISSUE_TEMPLATE/bug_report.md)与私密安全反馈入口。用户反馈提供最小复现及脱敏版本信息。

本次审查修复的源码检查和本机结果见 [修复验收](results/REVIEW_HARDENING.md)。签名、公证和跨架构验收不由此清单自动完成。
