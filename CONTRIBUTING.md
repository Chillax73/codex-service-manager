# 参与贡献

这是独立第三方开发工具。提交小范围、可复现的改动，先描述具体场景、当前行为、目标行为和影响。

## 开发文档

项目采用 self 流程。需求或契约有变化时，用 `self-spec` 明确要求与验收场景，按需补设计。用 `self-plan` 拆分可验证任务，再用 `self-implement` 实施、`self-review` 验收。范围清楚的局部修复可直接实现和回归。

| 要查或更新的内容 | 维护位置 |
| --- | --- |
| 产品目标与已确认的约束 | [产品需求](docs/PROJECT_REQUIREMENTS.md) |
| 当前行为、兼容要求和验收场景 | [主规格索引](specs/001-service-manager/spec.md)、[现行能力规格](docs/specs/) |
| 模块职责、技术决策、数据与接口 | [设计](specs/001-service-manager/design.md)、[数据模型](specs/001-service-manager/data-model.md)、[接口契约](specs/001-service-manager/contracts.json) |
| 实施依赖、完成条件和任务进度 | [实施计划](specs/001-service-manager/plan.md)、[任务清单](specs/001-service-manager/tasks.md) |
| 实际执行的验证、结果和覆盖边界 | [验收索引](specs/001-service-manager/acceptance.md)、`docs/results/` |

新变更使用 `docs/changes/<变更名>/spec.md` 和 `task.md`，涉及实质设计决策时补 `design.md`。任务引用对应的需求或场景，验收引用实际证据。变更验收通过后同步已确认的契约到 `docs/specs/<能力名>/spec.md`，再移入 `docs/changes/archive/`。使用 self 技能附带的 `self_check.py` 校验规格、变更和任务结构。

已在 `specs/001-service-manager/` 立项的工作沿用原任务编号及进度。较早各轮的计划、设计和验收见 [历史验收索引](specs/001-service-manager/acceptance.md#已完成变更归档)。

## 开发与验证

按 README 构建，使用 scripts/csm 登记长期测试服务。运行 `tests/test_support.py`、`tests/test_endpoints.py`、`tests/test_engine.py` 和 `tests/test_projects.py`；采集改动运行 `tests/test_capture_shell.py`、`tests/test_capture.py --suite core`（完整项目另用 `--suite full`）；存储改动运行 `tests/test_maintenance.py`，版本修改运行 `scripts/check_release.py`，容器采集需真实 `tests/verify_capture_docker.py`，步骤见 [复现与 CI](docs/TESTING.md)。修改服务模型时运行相关真实项目或 Docker 集成，修改界面时重新构建并检查实际窗口。只清理测试自己拥有的资源；停止失败禁止重启。GUI、菜单栏、真实 Codex 退出需要解锁桌面，不能把模拟测试写成真实验收。

基础 CI 自动运行；Docker 工作流由维护者手动执行。CI 不上传原始日志、数据库、截图或环境文件，不能证明桌面交互与真正 Codex 退出。

## 提交前

使用 GitHub noreply 邮箱，并检查 `git diff --cached`。运行 `/usr/bin/python3 scripts/check_publish.py`；截图需仅包含测试数据并按 [图片记录](docs/images/README.md)人工复核。避免完整环境、真实项目路径、原始数据库/日志、聊天 ID、启动参数或 URL 中的凭据。Issue 与 PR 使用模板；安全问题使用 [SECURITY.md](SECURITY.md) 的私密入口。

新引入代码、依赖或图片必须记录来源和授权，保留适用的许可/版权声明，更新 [依赖清单](THIRD_PARTY_NOTICES.md)。提交者确认拥有贡献权利并同意贡献以 MIT 分发；第三方材料保留其原许可。不要增加 OpenAI 或其他组织的官方标识。
