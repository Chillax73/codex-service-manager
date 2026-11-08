---
name: openspec-update-change
description: 修订 OpenSpec 变更中已有的规划产物并保持一致。用于调整计划、纳入新决策或协调文档，也响应 openspec update change、opsx update；若用户指刷新生成文件的 openspec update CLI 命令，则运行该命令。绝不修改代码。
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

修订变更中已经存在的规划产物，并保持它们相互一致。绝不修改实现代码。

**存储库选择：** 如果用户指定了 store（在本机注册的独立 OpenSpec 仓库），或当前工作位于某个 store 中，先运行 `openspec store list --json` 查找已注册的 store ID，然后为读取或写入规格和变更的命令（`new change`、`status`、`instructions`、`list`、`show`、`validate`、`archive`、`doctor`、`context`、`schemas`、`view`）添加 `--store <id>`。选定后，在整个工作流程中持续使用该参数。下文未带作用域的命令只是简写，执行前必须补上，例如执行 `openspec status --change "<name>" --json --store "<id>"`。其他命令不接受该参数。CLI 提示中的命令已包含此参数，后续使用时保持它。未选择 store 时，命令作用于最近的本地 `openspec/` 根目录。

**项目检查：** 本流程面向已经使用 OpenSpec 的项目。首次写入之前（包括 `new change`、`archive`、同步规格或编写产物文件），运行 `openspec list --json` 并读取 `root`；已选择 store 时添加 `--store <id>`，因为该 store 就是根目录。`root` 为对象表示已完成配置；`"root": null` 表示未配置，此处没有 `openspec/` 目录，`openspec new change` 等写入操作会附带创建它。此时命令也会以非零状态退出，这是检查结果，不代表 CLI 损坏；读取 JSON，不要重试或绕过检查。

有一种 `"root": null` 并非未配置：如果 `status` 错误信息以 `Declared in` 或 `Invalid store declaration in` 开头，并指向项目的 `openspec/config.yaml`（或 `config.yml`），说明项目通过声明的 store 使用 OpenSpec，但本机无法解析该声明（store 未注册或 `store:` 格式错误）。不要将它视为未初始化，也不要进入下面的分支；停止写入，向用户展示错误中的 `message` 和 `fix`。

除此之外，没有根目录时，按进入本流程的方式处理：

- **自动选择本技能：** 用户没有提及 OpenSpec、本技能或其斜杠命令，是你自行选择了它。停止使用 OpenSpec，按未安装 OpenSpec 的方式正常处理请求；不要要求用户配置，也不要提及 OpenSpec 初始化。
- **用户明确请求 OpenSpec：** 用户提及了 OpenSpec、本技能或运行了其斜杠命令。在写入前停止，询问是初始化项目（`openspec init`）、使用已有 store（`--store <id>`），还是本次继续但不使用 OpenSpec，并等待回答。

这两个分支都不能附带创建根目录：用户未要求初始化时，不运行 `openspec init`，不手工创建 `openspec/` 文件，也不让其他命令创建它。

**输入：** 可选提供变更名称。未提供时，尝试从对话上下文推断；描述含糊或存在歧义时，必须展示可用变更并请用户选择。

本流程修订已有产物，不负责补建完全缺失的产物。缺失时，`openspec status --change "<name>" --json` 会指出下一个产物，`openspec instructions "<artifact-id>" --change "<name>" --json` 会说明编写方式。已部分生成的通配符产物可按第 4 步的例外补建文件。

## 操作步骤

### 1. 选择变更

用户已提供名称时直接使用。否则：

- 从对话中提及的变更推断。
- 只有一个活动变更时自动选择。
- 存在歧义时，运行 `openspec list --json`，按最近修改时间排序，请用户选择。

提问时列出最近修改的 3～4 个变更，展示名称、状态（例如“0/5 个任务”“已完成”“无任务”）和 `lastModified` 表示的修改时间。将最近修改的标为“推荐”。

始终告知：“正在使用变更：<name>”，并说明可在 Codex 中用 `$openspec-update-change <other>`，其他代理中用 `/openspec-update-change <other>` 切换。

### 2. 获取变更产物

```bash
openspec status --change "<name>" --json
```

解析当前状态：

- `schemaName`：所用工作流模式，例如 `spec-driven`。
- `artifacts`：各产物及其状态（`done`、`skipped`、`ready`、`blocked`）。
- `isPlanningComplete`：所有规划产物是否完成；旧版 CLI 用 `isComplete` 表示同一值。
- `planningHome`、`changeRoot`、`artifactPaths`、`actionContext`：路径和范围上下文。以这些值为准，不假定路径位于当前仓库。

产物 ID 和路径来自当前模式；不要猜测，也不要按硬编码产物名称分支。自定义模式应能直接使用。

可编辑文件来自 `artifactPaths.<id>.existingOutputPaths`，它包含磁盘上真实存在且已展开通配符的文件（例如 `specs/**/*.md`），以及第 4 步允许且已确认的、部分生成的通配符产物中的具体新路径。不要写入通配符产物的 `resolvedOutputPath`，它仍是模式而非真实文件。

### 3. 理解请求

用户指定了具体修改（例如“设计现在改用 X”），以此为起点。仅说“更新”或“让内容一致”时，进行一致性审查，读取已有产物并相互比对，找出矛盾、缺口和重复。

### 4. 阅读并协调内容

- 读取请求涉及的产物及此变更的其他已有产物。
- 先在对话中起草修改，不写文件。明确影响范围，所有写入都在第 5 步进行。将草稿与其他已有产物双向核对：后续产物的修改也可能要求调整前置产物。构建顺序可作为阅读顺序，但不限制修订方向。
- 记录所有不一致、缺失或矛盾之处。
- 仅对已有文件（`existingOutputPaths`）提出修订。产物没有任何已有输出且状态为 `ready` 或 `blocked` 时，说明缺失，指向 `openspec instructions "<artifact-id>" --change "<name>" --json` 介绍创建方式。`skipped` 产物保持不动，不视为缺失，也不转交后续创建流程。
- 通配符产物（例如 `specs/**/*.md`）只要匹配至少一个文件就标记为 `done`，而继续创建流程只处理 `ready`。若一致性检查发现该产物还缺某个文件，且它的 `existingOutputPaths` 非空，按以下例外处理：
  1. 运行 `openspec instructions "<artifact-id>" --change "<name>" --json`，遵循 `instruction` 和 `template`。把 `context`、`rules` 当作约束，不复制进文件。返回 `skipped: true` 时不创建文件。重新从磁盘读取依赖文件；必需且未跳过的依赖缺失时，停止并请用户先恢复。
  2. 在 `changeRoot` 内选择一个与 `artifactPaths.<id>.outputPath` 匹配、尚不存在的具体路径。解析所有父目录符号链接后，确认路径仍位于 `changeRoot` 内。通配符形式的 `resolvedOutputPath` 不是有效目标。
  3. 将该新文件纳入第 5 步的修订提案，用户确认后才创建。
  4. 确认后、创建前，立即刷新状态和指令，确认产物仍在范围内、未跳过、且已有部分输出；重复上述具体路径检查。
  5. 使用目标已存在就失败的创建操作。若 `instruction` 委托其他技能或命令创建，仅在它能遵守已确认路径和这些约束时调用，否则停止。任何检查失败或已确认草稿失效时，停止并与用户协调，不覆盖已有内容，也不自行改选路径。
- 已有内容一致时，直接说明，无需提出修改。

### 5. 逐个产物确认并应用

本步骤承担本流程全部产物写入，前面的步骤不得编辑产物。

- 展示每项拟议修订及原因，包括第 4 步中用户所请求修改的草稿。用户确认后才写入。
- 用户拒绝某项修订时，不写入，保持对应产物不变。
- 需要大幅重写时，先获取该产物的规则和模板：

```bash
openspec instructions "<artifact-id>" --change "<name>" --json
```

### 6. 指出下一步，仅提供建议，不执行

- `existingOutputPaths` 为空且状态为 `ready` 或 `blocked`：运行 `openspec status --change "<name>" --json` 确认下一个产物，并指向 `openspec instructions "<artifact-id>" --change "<name>" --json` 介绍创建方式。
- 变更已经实施（任务已勾选或已经应用）：代码可能不再符合修订后的计划，建议使用 `$openspec-apply-change`（Codex）或 `/openspec-apply-change`（其他代理）落实差异。
- 规划和实施全部完成：建议 `$openspec-archive-change`（Codex）或 `/openspec-archive-change`（其他代理）。

## 输出

每次调用后说明：

- 修订了哪些产物，以及哪些拟议修订被拒绝。
- 在已有部分输出的通配符产物下创建了哪些文件。
- 因尚不存在而留待后续处理的产物：只能是无文件且为 `ready` 或 `blocked` 的产物，不包括 `skipped`。
- 变更当前状态及推荐的下一条命令。

## 约束

- 只处理规划产物，绝不编辑实现代码。修订意味着代码要变化时，停止并指向实施技能。
- 使用 `openspec status` 返回的产物 ID 和路径，不按硬编码名称分支。
- 只编辑 `existingOutputPaths` 的具体文件，以及第 4 步允许且已确认的部分通配符产物新路径；绝不写入通配符 `resolvedOutputPath`。
- 不推进产物构建边界：`existingOutputPaths` 为空且状态为 `ready` 或 `blocked` 时，创建工作属于其他流程。`skipped` 产物保持不动。唯一新文件例外是已有部分输出的通配符产物内，已确认的具体路径。
- 每项编辑均在用户确认后写入。
- 请求改变了变更本身的意图，而非细化原意时，请用户提供另一个未使用的名称，建议用 `openspec new change "<new-change-name>"` 新建，遵循“修订还是重新开始”的判断原则。
