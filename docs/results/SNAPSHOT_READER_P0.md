# P0 · 快照读取 CPU 优化验收

日期：2026-11-14。基线：`cef73e5`；范围：`CachedSnapshotConnection.readLine` 的重复换行搜索。

## 实现与兼容

旧实现每收到最多 8192 字节便从缓冲起点执行 `firstIndex(of:)`，搜索工作量随快照大小平方增长。新实现记录已扫描字节数，只对新区域调用 `memchr`，由 `Data.startIndex` 换算换行索引。跨调用保留下一帧残留，每次调用重新扫描残留，断线清空缓冲；不在重连间保留游标。

JSON 行协议、4 秒超时、64 MiB 缓冲上限、失败退避、2 秒可见窗口刷新均沿用原行为。此次未修改后台探测、采集策略、历史存储或 SwiftUI 状态发布。

## 回归证据

- `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_snapshot_reader.py`：编译实际生产连接类，12 类用例通过。包含分片、同批多帧/空帧、非零 Data 索引、残留加新片段、UTF-8 分字节、2 MiB 大帧、8192 字节边界、部分/空 EOF、4 秒超时、64 MiB 边界及超限、失败清空、退避、无效 JSON 后重连和同一 worker 后续读取。已加入 macOS CI；本次只宣称本地结果。
- `tests/test_snapshot_cache.py`：5 项通过。
- `tests/test_engine.py`：14 项真实进程回归通过；仅测试自有进程/端口。
- `scripts/build.sh` 与 `codesign --verify --deep --strict`：通过。
- 真实新版原生窗口截图显示 960 条测试服务及项目列表，刷新正常；原始截图只保存在本机临时目录。没有借用模拟窗口代替实际 UI。

## 对比方法

使用 `scripts/benchmark_snapshot_reader.py`，从固定基线和候选 Infrastructure.swift 分别编译原生应用，其余 Swift 源码、真实 Python backend、编译参数 `-O -swift-version 5` 相同。应用只注入相同的刷新计时/窗口状态记录及测试退出入口，不修改快照读取、刷新频率或 UI 内容。

环境：同一台 arm64 Mac、macOS 15.8.2、Swift 6.1.2。夹具为 24 个项目、960 条合成服务；两版共用同一隔离 SQLite 数据，项目自动探测全部暂停以隔离读取热路径，正式业务数据不参与。测试应用通过 `scripts/csm --root <测试登记目录> launch --task` 登记，退出后按服务 ID 清理。测试目录持有 watcher 锁以避免建立额外退出观察器，不影响正式观察器。

顺序为旧、新、新、旧（ABBA）；每轮至少 4 次成功刷新后预热结束，连续测量 24 秒。记录实际原生窗口与 snapshot-worker 各自用户态加内核态 CPU 时间增量；百分比采用单核 100% 口径，不含测试监督器及其他应用。

`proc_pid_rusage` 返回 Mach 时钟计数，按 `mach_timebase_info` 换算。本机比例 125/3，使用短时有界 CPU 校准与 `getrusage` 交叉核验：0.499979 秒与 0.499951 秒。Apple 的 [XNU 实现](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/bsd_kern.c) 中 `fill_task_rusage` 从 `task_power_info` 取得原始时间；不能沿用把计数直接视作纳秒的旧换算。最终脚本保存原始计数、比例与结果。

| 顺序 | 版本 | 窗口 CPU | worker CPU | 刷新次数 | 刷新耗时中位数 |
|---|---|---:|---:|---:|---:|
| 1 | 旧版 | 45.53% | 2.27% | 12 | 846.18 ms |
| 2 | 新版 | 5.73% | 2.13% | 12 | 65.55 ms |
| 3 | 新版 | 5.77% | 2.46% | 12 | 66.15 ms |
| 4 | 旧版 | 47.87% | 2.22% | 11 | 884.13 ms |

最终快照大小 **1,971,716 字节（约 1.97 MB）**。两轮均值：窗口 CPU **46.70% → 5.75%**，降低 **87.69%**；worker **2.24% → 2.29%**，基本不变。每轮刷新耗时中位数的均值 **865.15 → 65.85 ms**。最后一轮旧版在测量窗口内完成 11 次，其余为 12 次；两版定时器配置均为 2 秒，没有用降低新版刷新频率换取收益。

原始记录：本机 ignored 的 `.test-state/p0-cpu-final/results.json` 及四份刷新 JSONL；记录源文件 SHA-256，可核验旧版确为 `cef73e5`，候选与本次实现一致。

采集保护：前后均无采集器进程，hooks 关闭、62 个目录禁用；正式采集器历史运行次数保持 19，未新增运行。测试服务全部停止。


## 复现

前提：本机已安装管理器，正式采集器未运行、全局 hooks 已关闭；脚本只读核验这些前提，不替使用者更改设置。先构建候选应用，再从基线取出待比较文件，输出目录须不存在：

```sh
mkdir -p .test-state/p0-reference
git show cef73e5:app/Sources/Infrastructure.swift > .test-state/p0-reference/Infrastructure.swift
scripts/build.sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 scripts/benchmark_snapshot_reader.py \
  --baseline-infrastructure .test-state/p0-reference/Infrastructure.swift \
  --output .test-state/p0-reproduction
```

## 限制与审查

这是当前会话的范围内自审，不是独立审查。最初 3.13 MB 夹具使旧版刷新约 2 秒，错过定时周期，频率断言失败；该轮未作为最终对照。约 1.99 MB 试跑暴露计时单位问题后已修复换算，并用最终脚本重新测量。最终结论只使用最后的完整四轮数据。

此结果验证固定负载下的 P0 热路径收益。合成数据暂停自动探测，因此 worker CPU 不代表正式 935 条混合服务的后台探测成本，也不推断系统温度下降幅度。原 P0 实施阶段只构建，后续经用户明确授权完成部署，见下文；未推送。

## 正式部署核验（2026-11-14 10:34 CST）

将 `3033f12` 构建替换到 `/Applications/Codex Service Manager.app`，签名和二进制摘要一致，保留旧包备份。升级时主窗口已退出，原退出观察器保持原身份；新版窗口经 `scripts/csm` 登记为内部组件 `csm-desktop-manager` 后启动，实际窗口已显示。

真实数据预热后连续测量 30 秒（3 × 10 秒）：主窗口平均 CPU **4.57%**、snapshot-worker **11.17%**、自身监督器 **0.046%**、退出观察器 **0.071%**。10:34:18 的四个性能核心传感器（Tp01/Tp05/Tp09/Tp0D）平均 **57.78°C**、最高 **59.37°C**；传感器命名参考 [Stats 的 M2 映射](https://github.com/exelban/stats/blob/master/Modules/Sensors/values.swift)。温度受整机负载影响，不能直接与隔离 CPU 夹具混算。

Java、Node、OrbStack、退出观察器的出生身份未变，Java 18080 与 Node 5173 端口仍由原进程监听，5 个运行容器的 ID、启动时间和重启次数未变。无新增业务停止/重启操作。hooks 关闭、62 个目录策略内容完全一致，采集器运行次数仍为 19。原始记录保存在 ignored 的 `.test-state/p0-deployment/`。
