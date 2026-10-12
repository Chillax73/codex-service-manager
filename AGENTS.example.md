# 开发约定

用简体中文回答。需求基线：docs/PROJECT_REQUIREMENTS.md；实施设计：specs/001-service-manager/。

- 依次维护 spec、plan、tasks 与 acceptance，不把验证原型当完整交付。
- 新运行保存独立记录，停止失败不能继续重启；仅清理测试拥有的资源。
- 实际 UI 与桌面退出证据不能用模拟结果代替。
- 构建：scripts/build.sh。
- 进程验收：PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_engine.py。
- 变更服务模型后执行相关真实集成测试；修改界面后重新构建并检查实际窗口。
- 本机服务、后台任务和容器通过 scripts/csm 登记；不使用旧 PID 直接 kill。
- 聊天回复结束、切换或归档聊天不是应用退出，禁止调用 codex_exit。

把本文件复制为本机 AGENTS.md，再运行 scripts/integrate.py 安装当前克隆路径的接入指令。生成的绝对路径配置仅保留本机，不提交。
