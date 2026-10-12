# Codex Hooks 使用指南

本项目主要管理 Codex 启动的本机服务。正式链路是项目 AGENTS.md 引导 Codex 调用 scripts/csm，原生观察器处理真正应用退出。这里的 hooks 是可选事件实验；安装它们不会自动登记或停止服务。正常使用入口见 [README](../README.md#接入项目)。

## 准备与安装

安装支持 hooks 的 Codex CLI，确认 `codex --version` 可用。从本项目克隆根目录进行实验；安装器目前固定安装到本克隆 `.codex/hooks.json`，不接受目标项目参数，也不修改全局 hooks。不要把它当作 `scripts/integrate.py` 的替代品。

先阅读 `validation/install_hooks.py` 和 `validation/hook_recorder.py`，再执行：

```sh
/usr/bin/python3 validation/install_hooks.py
```

生成两个本机文件：项目 `.codex/hooks.json` 用于 Codex 加载，`validation/hooks.json` 是同一配置的检查副本。命令包含此克隆的绝对路径；移动克隆后需要更新本工具命令并重新审核。相同配置可重复安装，遇不同的既有配置会拒绝覆盖，不会删除别人的条目。遇到冲突可在独立克隆目录试验，或阅读脚本、审核后手动合并本工具 handler 至原配置的事件数组，保留已有内容。

## 审核与启用

在同一仓库根目录启动 `codex`，按正常流程信任项目，再在交互式 CLI 输入 `/hooks`。逐项核对来源是本项目 `.codex/hooks.json`，命令指向本克隆的 `validation/hook_recorder.py`，阅读脚本后才信任并启用。安装脚本没有授权功能，不会修改信任状态。

项目配置层需可信，非托管 hooks 需审核精确定义；新配置或定义变化会重新待审核。配置通常影响从此项目开始的新会话，不能假定旧桌面聊天已重新加载。[OpenAI 官方审核与配置说明](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)

## 配置探测与实际事件复验

在另一个终端、相同仓库根目录运行：

```sh
/usr/bin/python3 validation/verify_hooks.py --project-only
```

此脚本启动短时 app-server，通过配置协议探测五个实验 hooks 和信任元数据，不调用模型，不是实际桌面交互验收。结果写入本机 `validation/project-hook-results.json`。查看 configured_hook_count、trust_statuses、configuration_layers 与 error；退出码成功不等于 hooks 都已 trusted/enabled。最终核对 CLI `/hooks` 显示的精确来源与状态。

开启已加载配置的 Codex 会话，让 Codex 执行下列一次性无害命令：

```sh
CSM_POC_MARKER=readme python3 -c 'print("hook-probe")'
```

不要只在自己的终端执行：手动 shell 命令不会经过 Codex 的工具事件链。检查本机 `validation/.state/hook-events.jsonl`，观察实际事件是否出现、来源和 cwd 是否对应本次会话；不要仅凭配置已列出就认定事件已运行。CLI 实际事件与 Mac 桌面事件分别验证。

| 配置事件 | 本项目脚本行为 |
|---|---|
| SessionStart | 记录收到的会话开始元数据。 |
| PreToolUse / PostToolUse | 记录收到的工具事件元数据；仅含 CSM_POC_MARKER 的实验命令额外保存命令文本。 |
| Stop | 记录收到的轮次停止事件，不停止服务。 |
| SessionEnd | 记录收到的会话结束事件，不执行项目结束或 codex_exit。 |

官方 SessionEnd 可发生于会话归档/删除、Codex 正常关闭或符合条件的闲置，不等于 Mac 应用最后一个实例终止。不要将它直接接到正式服务停止逻辑。[OpenAI SessionEnd 说明](https://learn.chatgpt.com/docs/hooks#sessionend)

先前原型核验捕获过 CLI 的实际 SessionEnd，但没有确证当时桌面会话的 PreToolUse/PostToolUse；当前正式产品通过配套登记入口和原生退出观察器完成验收。这些覆盖范围见 [原型报告](../validation/ROUND1_REPORT.md) 与 [正式验收](results/VALIDATION.md)。本指南不声称此次已重新安装、审核或触发桌面 hooks。

## 排障与隐私

| 情况 | 处理 |
|---|---|
| 找不到 codex | 安装 CLI 并加入 PATH，先确认 codex --version；管理应用本身不需要 CLI 才能读已有记录。 |
| 安装器提示 Existing hooks differ | 保留已有文件；独立克隆试验或人工审核合并，不直接覆盖原配置。 |
| untrusted / 配置层禁用 | 在正常 Codex 流程信任项目，使用 /hooks 审核定义并核对是否启用。 |
| 已列出但无事件 | 开始新会话，确认 cwd、配置来源、启用状态和实际工具调用；分别验证 CLI 与桌面，不用配置探测替代事件证据。 |
| 路径有空格，计数不符 | 安装器会引用命令路径；当前探测脚本按命令字符串匹配，引用差异可能影响计数。用 /hooks 核对来源与命令，并检查实际事件。 |
| app-server 协议报错 | 探测入口使用实验配置接口，可能受 CLI 版本影响；查看本机 error/最小 stderr，使用 /hooks 检查实际配置。 |

记录器不读取聊天正文、完整环境或普通工具输出。它会记录事件名称、session_id/turn_id、cwd、tool_name/reason、接收时间，包含标记的实验命令也会落盘。元数据仍可能暴露路径和会话标识；标记命令不得含凭据。配置、原始 JSON/JSONL、stderr 仅留本机，反馈只提供人工脱敏的必要片段。

## 禁用与移除

1. 在 CLI `/hooks` 禁用本工具 handler，并对加载过配置的会话确认新配置已生效。
2. 检查项目 `.codex/hooks.json`，仅移除命令指向本克隆 `validation/hook_recorder.py` 的条目，保留同一事件中的其他 handler。如果整个文件只有本工具配置，可移除该文件；不要删除共享 `.codex/` 或其他设置。
3. 本机检查副本 `validation/hooks.json` 和实验事件 `validation/.state/hook-events.jsonl` 可在确认归属并备份需要的记录后删除。`.state/` 还可能含其他原型诊断，不要整目录清空。
4. 移除 hooks 不会停止已登记服务，也不会移除正式 AGENTS.md 接入；这两件事分别按 [使用与卸载](OPERATIONS.md) 操作。
