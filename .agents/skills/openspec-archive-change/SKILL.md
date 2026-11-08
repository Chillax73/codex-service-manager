---
name: openspec-archive-change
description: 在实验工作流中归档已完成的 OpenSpec 变更。用于实施完成后的收尾与归档，也响应 openspec archive、opsx archive。
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

在实验工作流中归档已经完成的变更。

**存储库选择：** 如果用户指定了 store（在本机注册的独立 OpenSpec 仓库），或当前工作位于某个 store 中，先运行 `openspec store list --json` 查找已注册的 store ID，然后为读取或写入规格和变更的命令（`new change`、`status`、`instructions`、`list`、`show`、`validate`、`archive`、`doctor`、`context`、`schemas`、`view`）添加 `--store <id>`。选定后，在整个工作流程中持续使用该参数。下文未带作用域的命令只是简写，执行前必须补上，例如执行 `openspec status --change "<name>" --json --store "<id>"`。其他命令不接受该参数。CLI 提示中的命令已包含此参数，后续使用时保持它。未选择 store 时，命令作用于最近的本地 `openspec/` 根目录。

**项目检查：** 本流程面向已经使用 OpenSpec 的项目。首次写入之前（包括 `new change`、`archive`、同步规格或编写产物文件），运行 `openspec list --json` 并读取 `root`；已选择 store 时添加 `--store <id>`，因为该 store 就是根目录。`root` 为对象表示已完成配置；`"root": null` 表示未配置，此处没有 `openspec/` 目录，`openspec new change` 等写入操作会附带创建它。此时命令也会以非零状态退出，这是检查结果，不代表 CLI 损坏；读取 JSON，不要重试或绕过检查。

有一种 `"root": null` 并非未配置：如果 `status` 错误信息以 `Declared in` 或 `Invalid store declaration in` 开头，并指向项目的 `openspec/config.yaml`（或 `config.yml`），说明项目通过声明的 store 使用 OpenSpec，但本机无法解析该声明（store 未注册或 `store:` 格式错误）。不要将它视为未初始化，也不要进入下面的分支；停止写入，向用户展示错误中的 `message` 和 `fix`。

除此之外，没有根目录时，按进入本流程的方式处理：

- **自动选择本技能：** 用户没有提及 OpenSpec、本技能或其斜杠命令，是你自行选择了它。停止使用 OpenSpec，按未安装 OpenSpec 的方式正常处理请求；不要要求用户配置，也不要提及 OpenSpec 初始化。
- **用户明确请求 OpenSpec：** 用户提及了 OpenSpec、本技能或运行了其斜杠命令。在写入前停止，询问是初始化项目（`openspec init`）、使用已有 store（`--store <id>`），还是本次继续但不使用 OpenSpec，并等待回答。

这两个分支都不能附带创建根目录：用户未要求初始化时，不运行 `openspec init`，不手工创建 `openspec/` 文件，也不让其他命令创建它。

`<capability-path>` 是相对于 `specs/` 的规格目录，例如 `user-auth` 或 `identity/user-auth`。查找对应主规格时，保留每份增量规格的完整能力路径。

**输入：** 可选提供变更名称。未提供时尝试从对话推断；含糊或有歧义时，必须展示可用变更并请用户选择。

## 操作步骤

### 1. 选择变更

用户提供名称时直接使用。否则从对话推断；只有一个活动变更时自动选择；有歧义时运行 `openspec list --json` 并请用户选择。只展示活动变更，不展示已归档变更；可用时附上各自模式。

始终告知：“正在使用变更：<name>”，并说明可用 `$openspec-archive-change <other>`（Codex）或 `/openspec-archive-change <other>`（其他代理）切换。

**在已有归档检查前加载当前归档输入：**

解析选定变更和规划根目录后，运行：

```bash
openspec instructions archive --change "<name>" --json
```

保留相同的根目录选择参数。此查询是可选的辅助输入，绝不能因此阻塞归档。非零退出或返回无效 JSON（例如旧 CLI 尚不支持）时，不加载上下文和操作建议，继续归档流程；不报错，也不停止。

成功响应也可能没有两个可选字段。返回 `context` 时，将其作为必须考虑的提示级输入，阅读并应用相关项目事实、约定和约束。`operationGuidance` 是可选的补充建议，逐条阅读考虑，只遵循适用且与内置归档流程兼容的条目。

将这两个字段与内置步骤、用户明确选择、已解析路径、CLI 检查和命令约定分开。上下文与这些控制输入冲突时，报告冲突并保持控制输入；建议不适用或有冲突时，不遵循并解释原因。不根据这两个字段推导替代路径、跳过确认或添加参数。除非用户另行要求，不将原文复制进规格、变更产物或归档总结。这些是提示级行为约定，不是可强制执行的检查。

### 2. 检查产物完成状态

运行 `openspec status --change "<name>" --json`，解析：

- `schemaName`：所用工作流模式。
- `planningHome`、`changeRoot`、`artifactPaths`、`actionContext`：路径和范围上下文。
- `artifacts`：产物及其状态（`done`、`skipped` 或其他）。

若有产物既非 `done` 也非 `skipped`，展示未完成产物警告，询问是否继续，用户确认后才继续。`skipped` 满足条件，因为变更声明了 `skip_specs`。

### 3. 检查任务完成状态

用相同的根目录选择参数运行 `openspec list --json`，从 `changes` 中查找 `name` 与所选变更完全匹配的条目。

必须恰好匹配一条；`totalTasks`、`completedTasks` 必须为非负整数，且 `completedTasks <= totalTasks`。由 CLI 解析该模式配置的任务文件，包括自定义产物名、输出路径和通配符。未完成数为 `totalTasks - completedTasks`。

不从产物状态或缺少顶层 `tasks.md` 推断任务完成。查询失败、JSON 无效、所选条目缺失或重复、计数无效时，报告问题，在同步或归档前停止。CLI 只有 `x` / `X` 复选框标记算完成，其他标记（包括不熟悉的标记）都算未完成。

- 有未完成任务：警告并展示数量，询问是否继续；确认后继续。
- `totalTasks` 为零：继续，不展示任务相关警告。

### 4. 评估增量规格同步状态

仅使用状态 JSON 中 `artifactPaths.specs.existingOutputPaths` 作为增量来源。缺少 `specs` 条目或路径列表为空时，不提示同步，继续归档；不从其他产物推测。

存在增量规格时，将每份增量与 `<planningHome.root>/openspec/specs/<capability-path>/spec.md` 对比。根目录使用第 2 步返回、支持 store 的 `planningHome.root`，不硬编码仓库路径。

**主规格缺失不自动等于“已同步”。** 对新能力，主规格是同步输出，不是前置输入：

- 增量含 MODIFIED 或 RENAMED：报告仅 ADDED 能创建新主规格，将该能力标为同步阻塞；不编造没有当前版本的需求。
- 否则，只有 REMOVED 且 `.openspec.yaml` 声明 `retire_capabilities: true`：能力已退役，算已同步，警告无内容可再删除，不重建主规格。本规则既用于此处，也用于同步后的验证。
- 否则，没有 ADDED：报告无法同步并标记阻塞。只有 REMOVED 时，警告无主规格可供删除，主规格目录树保持不动。无退役标记的纯 REMOVED，`openspec archive` 会以 `Spec must have at least one requirement` 拒绝。
- 否则，标为需要同步，并在总结中列出“<capability-path>：将创建新主规格”。同时含 REMOVED 时，警告因无主规格而忽略删除；只从 ADDED 创建主规格，与 `openspec archive` 一致。

确定新增、修改、删除和重命名操作。即使某能力同步阻塞，也继续评估其他能力；提问前展示合并总结。

**提供选项：**

- 任一能力同步阻塞：解释原因，只提供“跳过同步并归档”“取消”。
- 否则需要变化：提供“现在同步（推荐）”“跳过同步并归档”。
- 否则已同步：提供“立即归档”“仍然同步”“取消”。

按答案处理：

- “取消”：停止，不归档。
- “跳过同步并归档”或“立即归档”：进入归档。
- “现在同步”或“仍然同步”：同步后验证。存在同步阻塞能力时不能开始任何同步，解释阻塞并重新展示允许的选项。
- 其他答案：再次询问，不直接归档。

**同步前获取规则：** 选定同步后，写任何主规格之前，用相同根目录选择参数执行一次：

```bash
openspec instructions specs --change "<name>" --json
```

必须零退出且返回有效的产物指令 JSON。失败或无效时，报告错误，在写主规格或移动变更前停止。有效响应未含 `rules` 视为没有规则。返回规则仅约束本次合并生成的主规格内容和形式，不能用作归档操作建议，不能改变 CLI 行为，也不把规则原文复制进任何输出文件。

然后针对变更 `<name>`，在当前流程内运行 `openspec-sync-specs` 的代理智能合并流程，传入前面的增量分析和规格规则快照，等待完成。行内同步必须复用快照，不重复获取规格指令。不能交给后台任务，因为第 5 步会移动 `changeRoot`，让仍在读取的同步失去目录，最终变更已归档却未更新主规格。如果代理只能通过委派执行，则必须同步委派并等待结果。

同步报告任何停止或阻塞条件时，视为失败，立即停止归档；不做后续内容比较，也不移动 `changeRoot`。变更尚未移动，用户可修复阻塞或重新同步。

**同步成功写入后，检查主规格的规范结构：**

- 新主规格以 `# <capability> Specification` 开头；已有主规格标题逐字保持不变。
- 已有主规格的 `## Purpose` 整节完全不动。
- 新主规格逐字复制增量中的 `## Purpose`；用途文本短于标准校验要求时才警告，不重新生成或改写已有作者编写的内容。没有可用 Purpose 时，沿用 TBD 占位与警告行为。
- 主规格不残留 `## ADDED Requirements`、`## MODIFIED Requirements`、`## REMOVED Requirements`、`## RENAMED Requirements` 等增量标题，严格遵循同步格式规则。
- 本次写入或变更的需求块使用 `### Requirement:`，其场景使用 `#### Scenario:`，都位于 `## Requirements` 下。增量未提及的内容逐字保留。

随后按本步骤开头的规则，重新比较 `artifactPaths.specs.existingOutputPaths` 中每项能力，不仅检查同步流程声称改过的能力；同时包含显式退役且主规格缺失的情况。成功后不应有剩余操作，每个能力都应显示已同步：

- ADDED 需求已存在。
- MODIFIED 需求包含增量指定的场景和描述变化，其他场景完整保留。
- REMOVED 需求已消失；若本次删除最后一个需求使能力退役，应删除主规格，而不是留下空 `## Requirements`。
- RENAMED 需求以新名称存在，旧名称不存在。

同步失败或任何能力不匹配时，报告差异并停止，不归档。`changeRoot` 仍保持原位，用户可修复差异或重新同步，再发起归档。

### 5. 执行归档

`planningHome.changesDir` 下没有 `archive` 时，创建：

```bash
mkdir -p "<planningHome.changesDir>/archive"
```

生成目标名：变更名已以 `YYYY-MM-DD-` 开头时直接使用；否则前缀当前日期，得到 `YYYY-MM-DD-<change-name>`。绝不叠加第二个日期，与 `openspec archive` 一致。

检查目标是否存在：

- 已存在：报错，建议重命名已有归档或改用其他日期。
- 不存在：将 `changeRoot` 移入归档目录。

```bash
mv "<changeRoot>" "<planningHome.changesDir>/archive/<target-name>"
```

### 6. 展示总结

说明变更名称、所用模式、归档路径、规格是否同步（适用时），以及未完成产物或任务等警告。

## 成功输出示例

```text
## 归档完成

变更：<change-name>
模式：<schema-name>
归档位置：<由 planningHome.changesDir 推导的 archive/<target-name>/ 路径>
规格：<仅第 4 步验证通过才写“✓ 已同步到主规格”；否则写“无增量规格”或“已跳过同步”>

<全部产物和任务完成时如实说明；带警告归档则列出，例如“归档时仍有 2 个任务未完成”>
```

## 约束

- 告知选中的变更，有歧义时请用户选择。
- 用产物图（`openspec status --json`）检查产物完成情况。
- 警告本身不阻止归档，但要告知并确认。
- 移动目录时保留随目录一起移动的 `.openspec.yaml`。
- 清楚总结实际结果。
- 请求同步时，在当前流程内运行 `openspec-sync-specs`；同步未结束时绝不归档。同步完成并验证主规格后才能移动 `changeRoot`。
- 有增量规格时，始终先评估同步状态，展示完整总结后再提问。
- 应用相关运行时上下文并报告冲突；操作建议仍仅是建议。逐条考虑，对不适用或冲突的条目解释原因。
- 保持已有 CLI 检查、解析路径、确认提示和命令约定。
- 产物规则仅约束写入的规格，绝不当作操作建议。
- 不把运行时上下文、操作建议或产物规则原文复制进输出文件。
