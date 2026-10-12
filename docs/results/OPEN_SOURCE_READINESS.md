# 开源收尾核验

日期：2026-10-12。公开仓库 codex-service-manager 使用独立脱敏初始提交与 MIT；旧仓库不再从 GitHub 查询到；用户确认该调整，原本机源码和历史保留。公开历史不包含旧私有提交；不声称原私有对象的 GitHub 缓存已清除。

| 工作 | 当前证据 |
|---|---|
| 历史/隐私 | 新仓库独立初始化，noreply 作者/提交者；Git 索引隐私扫描，原始证据 ignored。 |
| 来源/许可 | MIT；npm 锁文件 64 个组件的许可字段，Java 30 个运行组件许可清单；真实截图已查看并固定内容哈希。 |
| 复现 | 不含 ignored 目录、路径含空格的新源码副本：14 组进程与 9 组端点通过；目录/工具契约 3 组通过。 |
| 工具/夹具 | PATH npm/Maven，Vite/Spring/Python 启动、重启、停止与端口释放共 9 项通过，独立测试观察器已清理。 |
| 依赖公告 | Vite 7.3.7、source-map-js 1.2.2；npm audit 当前零告警。未将该结果扩大为所有 Java/系统组件的漏洞审计。 |
| 构建/截图 | 当前 Mac 构建与 ad-hoc 签名校验通过；README 截图为真实隔离测试窗口，不替代正式 UI/Codex 退出验收。 |
| 使用/协作 | 上手、卸载、排障、CONTRIBUTING、SECURITY、Issue/PR 模板已补齐；GitHub 私密漏洞报告已启用。 |
| 基础 CI | macOS 14/15 构建、签名、Python 回归/隐私与 npm 审核通过：[Actions](https://github.com/Chillax73/codex-service-manager/actions/runs/38224121884)。 |
| Docker CI | 独立 Linux runner 的 Docker/Compose 与真实失败场景通过：[Actions](https://github.com/Chillax73/codex-service-manager/actions/runs/38224127094)。 |
| 首版发布 | [v0.1.0 源码开发者预览](https://github.com/Chillax73/codex-service-manager/releases/tag/v0.1.0) 已发布；标签指向 `6a52478`，该提交 [CI 已通过](https://github.com/Chillax73/codex-service-manager/actions/runs/38224570507)。无应用二进制附件。 |

原有 A-01 至 A-16 是先前 Mac 的本机验收，见 [报告](VALIDATION.md)。此次没有改变产品服务模型或原生界面代码；CI 不声称拥有真实桌面或 Codex 退出证据。源码预览不包含 `.app`、Python 运行时、第三方依赖或用户状态。

发布后 main 的收尾改动仅记录完成状态和澄清平台验证范围，不改变 v0.1.0 的产品/测试代码。
