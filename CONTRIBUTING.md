# 参与贡献

这是独立第三方开发工具。提交小范围、可复现的改动，先描述具体场景、当前/期望行为和影响；需求基线在 docs/PROJECT_REQUIREMENTS.md，依次维护 spec、plan、tasks 和 acceptance。

## 开发与验证

按 README 构建，使用 scripts/csm 登记长期测试服务。运行 `tests/test_support.py`、`tests/test_endpoints.py`、`tests/test_engine.py` 和 `tests/test_projects.py`；采集改动运行 `tests/test_capture_shell.py`、`tests/test_capture.py --suite core`（完整项目另用 `--suite full`）；存储改动运行 `tests/test_maintenance.py`，版本修改运行 `scripts/check_release.py`，容器采集需真实 `tests/verify_capture_docker.py`，步骤见 [复现与 CI](docs/TESTING.md)。修改服务模型时运行相关真实项目或 Docker 集成，修改界面时重新构建并检查实际窗口。只清理测试自己拥有的资源；停止失败禁止重启。GUI、菜单栏、真实 Codex 退出需要解锁桌面，不能把模拟测试写成真实验收。

基础 CI 自动运行；Docker 工作流由维护者手动执行。CI 不上传原始日志、数据库、截图或环境文件，不能证明桌面交互与真正 Codex 退出。

## 提交前

使用 GitHub noreply 邮箱，并检查 `git diff --cached`。运行 `/usr/bin/python3 scripts/check_publish.py`；截图需仅包含测试数据并按 [图片记录](docs/images/README.md)人工复核。避免完整环境、真实项目路径、原始数据库/日志、聊天 ID、启动参数或 URL 中的凭据。Issue 与 PR 使用模板；安全问题使用 [SECURITY.md](SECURITY.md) 的私密入口。

新引入代码、依赖或图片必须记录来源和授权，保留适用的许可/版权声明，更新 [依赖清单](THIRD_PARTY_NOTICES.md)。提交者确认拥有贡献权利并同意贡献以 MIT 分发；第三方材料保留其原许可。不要增加 OpenAI 或其他组织的官方标识。
