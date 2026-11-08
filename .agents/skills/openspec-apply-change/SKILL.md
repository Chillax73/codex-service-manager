---
name: openspec-apply-change
description: 实施 OpenSpec 变更中的任务。用于开始实施、继续实施或逐项完成任务，也响应 openspec apply、opsx apply、openspec implement。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.16.0"
  compatibility: 需要 openspec CLI。
  locale: zh-CN
---

用简体中文沟通。环境要求：已安装 `openspec` CLI。

实施 OpenSpec 变更中的任务。

**存储库选择：** 如果用户指定了 store（在本机注册的独立 OpenSpec 仓库），或当前工作位于某个 store 中，先运行 `openspec store list --json` 查找已注册的 store ID，然后为读取或写入规格和变更的命令（`new change`、`status`、`instructions`、`list`、`show`、`validate`、`archive`、`doctor`、`context`、`schemas`、`view`）添加 `--store <id>`。选定后，在整个工作流程中持续使用该参数。下文未带作用域的命令只是简写，执行前必须补上，例如执行 `openspec status --change "<name>" --json --store "<id>"`。其他命令不接受该参数。CLI 提示中的命令已包含此参数，后续使用时保持它。未选择 store 时，命令作用于最近的本地 `openspec/` 根目录。

**项目检查：** 本流程面向已经使用 OpenSpec 的项目。首次写入之前（包括 `new change`、`archive`、同步规格或编写产物文件），运行 `openspec list --json` 并读取 `root`；已选择 store 时添加 `--store <id>`，因为该 store 就是根目录。`root` 为对象表示已完成配置；`"root": null` 表示未配置，此处没有 `openspec/` 目录，`openspec new change` 等写入操作会附带创建它。此时命令也会以非零状态退出，这是检查结果，不代表 CLI 损坏；读取 JSON，不要重试或绕过检查。

有一种 `"root": null` 并非未配置：如果 `status` 错误信息以 `Declared in` 或 `Invalid store declaration in` 开头，并指向项目的 `openspec/config.yaml`（或 `config.yml`），说明项目通过声明的 store 使用 OpenSpec，但本机无法解析该声明（store 未注册或 `store:` 格式错误）。不要将它视为未初始化，也不要进入下面的分支；停止写入，向用户展示错误中的 `message` 和 `fix`。

除此之外，没有根目录时，按进入本流程的方式处理：

- **自动选择本技能：** 用户没有提及 OpenSpec、本技能或其斜杠命令，是你自行选择了它。停止使用 OpenSpec，按未安装 OpenSpec 的方式正常处理请求；不要要求用户配置，也不要提及 OpenSpec 初始化。
- **用户明确请求 OpenSpec：** 用户提及了 OpenSpec、本技能或运行了其斜杠命令。在写入前停止，询问是初始化项目（`openspec init`）、使用已有 store（`--store <id>`），还是本次继续但不使用 OpenSpec，并等待回答。

这两个分支都不能附带创建根目录：用户未要求初始化时，不运行 `openspec init`，不手工创建 `openspec/` 文件，也不让其他命令创建它。

**输入：** 可选提供变更名称，例如在 Codex 中使用 `$openspec-apply-change add-auth`，在其他代理中使用 `/openspec-apply-change add-auth`。未提供时，尝试从对话上下文推断；描述含糊或存在歧义时，必须展示可用变更并请用户选择。

## 操作步骤

### 1. 选择变更

用户已提供名称时直接使用。否则：

- 用户在对话中提及某个变更时，从上下文推断。
- 只有一个活动变更时，自动选择。
- 存在歧义时，运行 `openspec list --json` 获取列表，请用户选择。

始终告知：“正在使用变更：<name>”，并说明如何切换，例如在 Codex 中使用 `$openspec-apply-change <other>`，在其他代理中使用 `/openspec-apply-change <other>`。

### 2. 检查状态，了解工作流模式

```bash
openspec status --change "<name>" --json
```

解析 JSON，确认：

- `schemaName`：使用的工作流模式，例如 `spec-driven`。
- `planningHome`、`changeRoot`、`actionContext`：规划范围和编辑约束。
- 哪个产物记录任务。`spec-driven` 通常使用 `tasks`，其他模式以状态输出为准。

### 3. 获取实施指令

```bash
openspec instructions apply --change "<name>" --json
```

返回内容包括：

- `contextFiles`：产物 ID 到具体文件路径数组的映射，随模式变化，可能是提案、规格、设计、任务，也可能是规格、测试、实现和文档。
- 总任务数、已完成数、剩余数。
- 带状态、源路径和源行号的任务列表。
- 根据当前状态生成的动态指令。
- 可选的 `context`：选定根目录提供的当前必需项目上下文。
- 可选的 `operationGuidance`：当前实施操作的建议。
- 存在时的 `missingArtifacts`：尚无输出的必需产物 ID。

按状态处理：

- `state: "blocked"`：展示消息，暂停实施。
  - `missingArtifacts` 非空时，建议补齐缺失产物。运行 `openspec status --change "<name>" --json`，选择下一个 `ready` 产物（不能选择 `skipped` 或 `blocked`），再运行 `openspec instructions "<artifact-id>" --change "<name>" --json` 获取规则和模板。两个命令都保留选定的 `--store <id>`。
  - 否则，根据 CLI 指令，利用已有规划产物创建或修复模式所配置的跟踪文件。不要假设其他产物已就绪，也不要在阻塞状态下开始实施。
- `state: "all_done"`：报告所有跟踪任务均已完成，建议按需审查或验证后再归档。
- 其他状态：继续实施。

将 `context` 作为必须考虑的提示级输入，阅读并应用其中相关的项目事实、约定和约束。`operationGuidance` 是可选的补充建议；逐条阅读考虑，只遵循适用且与内置流程兼容的条目。

这两个字段与 CLI 返回的状态、缺失产物、任务、进度、`contextFiles` 及内置 `instruction` 分开处理。它们不能证明任务完成，不能替代内置指令，也不能绕过阻塞状态。上下文与内置指令、用户明确选择或 CLI 控制值冲突时，报告冲突并保留具有控制权的值。建议不适用或与上述输入冲突时，不执行并解释原因。这些是提示级行为约定，不是可强制执行的校验。

### 4. 阅读上下文文件

读取实施指令输出中 `contextFiles` 列出的每一个文件路径。

- `spec-driven`：提案、规格、设计和任务。
- 其他模式：以 CLI 返回的 `contextFiles` 为准。

除非用户另行要求，不要把 `context` 或 `operationGuidance` 原文复制进实现文件或规划产物。

### 5. 展示当前进度

展示使用的模式、已完成 N/M 个任务、剩余任务概览和 CLI 的动态指令。

### 6. 逐项实施，直到完成或遇到阻塞

对每个待办任务：

1. 告知正在处理哪个任务。
2. 完成必要代码修改，保持改动最小且聚焦。
3. 修改复选框前，确认返回的 `sourcePath` 和 `line` 处的复选框仍与任务描述匹配；不匹配时重新获取实施指令，使用刷新后的位置。
4. 在该任务返回的 `sourcePath` 和 `line` 处标记完成：`- [ ]` → `- [x]`。
5. 重新运行实施指令，确认该任务已完成且进度已更新。
6. 继续下一项。

以下情况暂停：

- 任务不清楚：请求澄清。
- 实施暴露设计问题：建议更新产物。
- 需要超出规格和任务描述的工作，或为了适配实现而打算删减、缩小、推迟已规定行为或接受例外：说明新增范围并询问，不要默默吸收。
- 遇到错误或阻塞：报告并等待指导。
- 用户中断。

### 7. 完成或暂停时展示状态

列出本次完成的任务和总进度“已完成 N/M 个任务”。全部完成时，报告跟踪任务已完成，建议按需审查或验证后归档。暂停时，解释原因并等待指导。

## 实施中的输出示例

```text
## 正在实施：<change-name>（模式：<schema-name>）

正在处理任务 3/7：<任务描述>
[……正在实施……]
✓ 任务完成

正在处理任务 4/7：<任务描述>
[……正在实施……]
✓ 任务完成
```

## 完成时的输出示例

```text
## 实施完成

变更：<change-name>
模式：<schema-name>
进度：已完成 7/7 个任务 ✓

本次完成：
- [x] 任务 1
- [x] 任务 2
……

所有跟踪任务均已完成。请按需审查或验证后再归档。
在 Codex 中使用 $openspec-archive-change，其他代理中使用 /openspec-archive-change。
```

## 遇到问题暂停时的输出示例

```text
## 实施已暂停

变更：<change-name>
模式：<schema-name>
进度：已完成 4/7 个任务

遇到的问题：
<问题描述>

可选方案：
1. <方案 1>
2. <方案 2>
3. 其他方法

你希望如何处理？
```

## 约束

- 持续推进任务，直到完成或遇到阻塞。
- 开始前必须读取实施指令返回的上下文文件，不假定固定文件名。
- 任务有歧义、需求不清楚、遇到错误或阻塞时，暂停询问，不猜测。
- 实施暴露问题时，暂停并建议更新产物。
- 代码改动保持最小，限定在当前任务范围。
- 每完成一项立即更新复选框，使用该任务的 `sourcePath` 和 `line` 精确定位。
- 超出规格的工作要先说明新增范围并暂停；不能默默缩减、推迟或简化掉已规定行为。
- 只有规定行为全部实现，才能标记 `- [x]`；部分实现或推迟不算完成。
- 不把上下文或操作建议当作任务完成的证据。
- 应用相关项目上下文；与控制流程的输入冲突时报告。
- 考虑每条建议，对不适用或冲突的条目解释原因。
- 不把运行时上下文和操作建议复制进实现文件或规划产物。
- 保留 CLI 控制的阻塞、就绪、全部完成状态及完成标准。

## 与灵活工作流衔接

本技能支持“对变更执行操作”的模型：有任务时，即使产物未全部完成也可调用；可以在部分实施后调用，也可以与其他操作交替使用。实施中发现设计问题时，建议修订产物，不受固定阶段限制。
