# 开发约定

用简体中文回答。仓库使用 SwiftUI/AppKit、Python 标准库和 SQLite。开发文档入口为 `CONTRIBUTING.md`，主规格为 `docs/project/spec.md`。

- 按需使用 `self-spec`、`self-plan`、`self-implement`、`self-review`。现行能力规格位于 `docs/specs/`，新变更位于 `docs/changes/`，已有工作的进度和证据入口见 `CONTRIBUTING.md`。明确的局部修复可直接实现并验证。
- 新运行保存独立记录，停止失败不能继续重启；仅清理测试拥有的资源。
- 实际 UI 与桌面退出证据不能用模拟结果代替。
- 构建：scripts/build.sh。
- 进程验收：PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_engine.py。
- 变更服务模型后执行相关真实集成测试；修改界面后重新构建并检查实际窗口。
- 本机服务、后台任务和容器通过 scripts/csm 登记；不使用旧 PID 直接 kill。
- 聊天回复结束、切换或归档聊天不是应用退出，禁止调用 codex_exit。

本文件是本仓库开发指令模板。首次使用可复制为本机 `AGENTS.md`。服务接入的安装与移除见 `docs/INTEGRATION.md`，安装器生成的绝对路径指令仅留本机。
