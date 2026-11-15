# P1 · 减少无变化刷新

日期：2026-11-14。相邻基线为 P0 提交 `3033f12`。

## 实现

- ServiceItem 的业务内容相同就不重新发布 services；ProjectItem 同样忽略独立处理的检查时间。删除记录、排序选项、操作记录和组件状态也只在内容变化时发布。
- 检查时间通过独立 CheckTimes 驱动时间标签；刷新中提示通过 RefreshState 驱动局部文字。心跳的 updated 不进入业务状态比较，实际 alive、错误和事件数变化仍发布。
- 服务集合变化时建立按 ID 索引、项目汇总和全局计数；搜索、项目和状态条件变化时更新筛选结果。保持原来的 2/10 秒刷新周期与服务顺序。
- 原始完整快照与 JSON 传输暂未缩减，属于 P2 范围。

## 回归

`tests/test_store_snapshot.py` 编译真实 Models/Status/Store/Infrastructure，核验重复快照不发布、时间/心跳/加载状态独立、最新时间保留、筛选交集、过期/不可用/恢复、暂停、运行切换、删除与更新后的汇总，以及选择/历史日志保持。260、1000、5000 条记录均通过；稳定快照应用耗时分别约 4.33、10.68、52.65 ms，仅为本地短测，不当作长期 CPU 结果。

`tests/verify_snapshot_native.py` 在真实原生窗口中通过 7 项检查：260 条数据加载、三次周期刷新保留选择和日志滚动、手动刷新保留日志、真实 worker 终止后显示不可用、恢复 worker 后状态恢复、菜单驻留继续刷新、重开窗口仍保留选择及滚动。实际日志滚动位置在这些操作后仍为 400。仅测试自身窗口、worker 与隔离数据库；未启用采集器。原始记录位于 ignored 的 `.test-state/native-snapshot-1794624518700075000/`。

原生构建通过，新增 Store 测试已接入 CI；本次没有运行远端 CI。

## 相邻版本 CPU 对照

同机 arm64/macOS 15.8.2，两个版本均使用 `-O`、同一真实 Python 后端、24 项目/1000 条暂停探测的合成服务，快照约 2.06 MB。窗口始终可见，预热至少 4 次成功刷新，再按 P0/P1/P1/P0 顺序各测量 60 秒；每轮均 30 次刷新。通过 Mach 时钟比例 125/3 换算，单核 100% 口径；保留完整源文件摘要和原始计数。

| 轮次 | 版本 | 主窗口 CPU | worker CPU | 刷新耗时中位数 |
|---|---|---:|---:|---:|
| 1 | P0 | 5.76% | 2.35% | 66.28 ms |
| 2 | P1 | 2.76% | 2.00% | 80.41 ms |
| 3 | P1 | 3.65% | 1.61% | 64.33 ms |
| 4 | P0 | 5.54% | 1.94% | 58.08 ms |

两轮均值：主窗口 **5.65% → 3.21%（下降 43.3%）**；窗口与 worker 合计 **7.80% → 5.01%（下降 35.7%）**。后端未改，worker 的差异不归因于 P1。刷新耗时中位数的均值 **62.18 → 72.37 ms**，增加约 10 ms：差异比较本身有成本，收益主要来自减少整份界面刷新。P2 将继续减少解码和比较的内容。

原始结果为 `.test-state/p1-cpu-window/results.json`。同负载基线是本次重新运行的 P0，而非直接与此前 960 条记录的 P0 报告相减。此阶段菜单模式验证了行为，未声称已测菜单 CPU 降幅。

复现：保存基线全部 Swift 文件及对应 `Codex Service Manager.app` 到独立目录，然后运行：

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 scripts/benchmark_snapshot_reader.py \
  --baseline-directory .test-state/p1-baseline \
  --output .test-state/p1-reproduction --services 1000 --seconds 60
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_store_snapshot.py
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/verify_snapshot_native.py
```

## 自审与边界

本阶段自审 PASS：时间及故障提示保留，业务内容与控制能力变化仍更新，列表/选择/日志回归通过。正式采集器持续关闭，hooks 关闭、62 个目录策略及 19 次历史运行保持不变。P1 未替换已部署的 P0 应用；后续阶段验证完成后再交付最终构建，不影响当前业务服务。
