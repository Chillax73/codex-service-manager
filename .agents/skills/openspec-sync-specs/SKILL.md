---
name: openspec-sync-specs
description: 将 OpenSpec 变更中的增量规格同步到主规格。用于更新主规格但暂不归档变更，也响应 openspec sync、opsx sync。
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

将变更中的增量规格同步到主规格。

这是**由代理执行的操作**：读取增量规格，直接编辑主规格以应用变化。通过智能合并，可以只增加一个场景，而非覆盖整个需求。

**存储库选择：** 如果用户指定了 store（在本机注册的独立 OpenSpec 仓库），或当前工作位于某个 store 中，先运行 `openspec store list --json` 查找已注册的 store ID，然后为读取或写入规格和变更的命令（`new change`、`status`、`instructions`、`list`、`show`、`validate`、`archive`、`doctor`、`context`、`schemas`、`view`）添加 `--store <id>`。选定后，在整个工作流程中持续使用该参数。下文未带作用域的命令只是简写，执行前必须补上，例如执行 `openspec status --change "<name>" --json --store "<id>"`。其他命令不接受该参数。CLI 提示中的命令已包含此参数，后续使用时保持它。未选择 store 时，命令作用于最近的本地 `openspec/` 根目录。

**项目检查：** 本流程面向已经使用 OpenSpec 的项目。首次写入之前（包括 `new change`、`archive`、同步规格或编写产物文件），运行 `openspec list --json` 并读取 `root`；已选择 store 时添加 `--store <id>`，因为该 store 就是根目录。`root` 为对象表示已完成配置；`"root": null` 表示未配置，此处没有 `openspec/` 目录，`openspec new change` 等写入操作会附带创建它。此时命令也会以非零状态退出，这是检查结果，不代表 CLI 损坏；读取 JSON，不要重试或绕过检查。

有一种 `"root": null` 并非未配置：如果 `status` 错误信息以 `Declared in` 或 `Invalid store declaration in` 开头，并指向项目的 `openspec/config.yaml`（或 `config.yml`），说明项目通过声明的 store 使用 OpenSpec，但本机无法解析该声明（store 未注册或 `store:` 格式错误）。不要将它视为未初始化，也不要进入下面的分支；停止写入，向用户展示错误中的 `message` 和 `fix`。

除此之外，没有根目录时，按进入本流程的方式处理：

- **自动选择本技能：** 用户没有提及 OpenSpec、本技能或其斜杠命令，是你自行选择了它。停止使用 OpenSpec，按未安装 OpenSpec 的方式正常处理请求；不要要求用户配置，也不要提及 OpenSpec 初始化。
- **用户明确请求 OpenSpec：** 用户提及了 OpenSpec、本技能或运行了其斜杠命令。在写入前停止，询问是初始化项目（`openspec init`）、使用已有 store（`--store <id>`），还是本次继续但不使用 OpenSpec，并等待回答。

这两个分支都不能附带创建根目录：用户未要求初始化时，不运行 `openspec init`，不手工创建 `openspec/` 文件，也不让其他命令创建它。

`<capability-path>` 是相对于 `specs/` 的规格目录，例如 `user-auth` 或 `identity/user-auth`。解析对应主规格时，保留每份增量规格的完整能力路径。

**输入：** 可选提供变更名称。未提供时，尝试从对话推断；含糊或有歧义时，必须展示可用变更并请用户选择。

## 操作步骤

### 1. 选择变更

已提供名称时直接使用。否则从对话推断；只有一个活动变更时自动选择；有歧义时运行 `openspec list --json` 并请用户选择。提问时展示包含增量规格（`specs/` 目录下）的变更。

始终告知：“正在使用变更：<name>”，并说明可用 `$openspec-sync-specs <other>`（Codex）或 `/openspec-sync-specs <other>`（其他代理）切换。

### 2. 解析变更上下文

```bash
openspec status --change "<name>" --json
```

JSON 包含 `planningHome.root`。主规格位于 `<planningHome.root>/openspec/specs/`。后续所有主规格路径都使用这个支持 store 的根目录，不硬编码仓库路径；选定 store 后，它指向 store 而非当前仓库。

### 3. 查找增量规格

状态 JSON 的 `artifactPaths.specs.existingOutputPaths` 是增量规格路径的唯一来源。缺少 `specs` 条目或 `existingOutputPaths` 为空时，告知无增量规格可同步并停止；不从其他产物推测，也不请求产物指令或写入主规格。

默认同步 `existingOutputPaths` 中每个路径，除非调用方明确缩小集合。缩小范围必须给出该数组中完整条目的显式列表，逐字使用这些绝对路径。归档可在行内调用时如此指定，用户也可以，例如选择以 `/specs/billing/invoices/spec.md` 结尾的条目。

只同步指定路径，其他增量规格保持不动。批量归档可能排除了未找到对应实现的增量；自行同步它会写入调用方刻意保留的主规格。第 4 步持续使用这个子集，不扩大回全部列表。指定路径不属于 `existingOutputPaths` 时，报告并停止，不默默忽略。指定列表为空时，报告无可同步内容并停止，不写主规格。

每份增量规格可包含：

- `## ADDED Requirements`：新增需求。
- `## MODIFIED Requirements`：修改已有需求。
- `## REMOVED Requirements`：删除需求。
- `## RENAMED Requirements`：重命名需求，采用 `FROM:` / `TO:` 格式。

没有找到增量规格时，告知并停止。

### 4. 将每份增量合并到主规格

首次写主规格前，获取一次当前规格规则快照：

- 归档流程行内调用并提供了 `openspec instructions specs --change "<name>" --json` 的有效快照时，复用，不重复获取。
- 其他情况，现在执行该命令一次，保留相同的根目录选择参数。
- 直接查询非零退出或返回无效的产物指令 JSON 时，报告错误并在写任何主规格前停止。不能将失败当作没有规则。
- 有效响应未包含 `rules` 表示没有配置产物规则，继续原有语义合并。

返回的 `rules` 仅约束本次合并产生的主规格内容和形式，不是操作建议，不能改变已选根目录、增量路径、CLI 检查或流程步骤。把原文当作约束，不复制到主规格或总结。

对第 3 步选定的每个能力增量路径执行以下操作；它可能位于 store 而非仓库。

#### a. 阅读增量规格

理解预期变化。

#### b. 阅读对应主规格

路径为 `<planningHome.root>/openspec/specs/<capability-path>/spec.md`，可能尚不存在。

主规格不存在时，与 `openspec archive` 保持一致：只有 ADDED 需求可应用，由步骤 d 创建。MODIFIED、RENAMED 没有可操作的当前需求，停止该能力同步，报告主规格不存在且新规格只允许 ADDED，绝不编造缺失需求。REMOVED 无内容可删除，跳过并警告。

#### c. 智能应用变化

**ADDED Requirements：** 主规格中不存在时新增；已存在时更新为目标内容，视为隐式 MODIFIED。

**MODIFIED Requirements：** 查找对应需求，增加缺失场景、修改已有场景或修改需求描述；保留增量未提及的场景与内容。

**REMOVED Requirements：** 删除完整需求块。只有以下条件全部满足，才删除整个 `spec.md`，并在目录无其他内容时删除该目录，从而退役能力：

1. 本次删除需求后，确实不剩任何需求块。
2. 规格其余结构有效，仍有 `## Purpose`。
3. 同步前主规格不是已经为空；本次没有删除任何内容时，就不做变化。
4. 整个文件中每个其他非空行，都可归属于标题、Purpose、Requirements 标题，或规范需求的陈述、场景、围栏示例。
5. 变更的 `.openspec.yaml` 声明 `retire_capabilities: true`。
6. `spec.md` 解析后仍位于真实规格根目录内；不能跟随能力目录符号链接删除外部文件。

如果删除选定需求会让需求块清空，但任一退役条件不满足，不修改主规格。停止该能力同步，报告阻塞条件及解决方式，绝不能写入或留下空的 `## Requirements`。只缺退役标记时，也明确说明用户可以通过添加该标记来允许退役。

删除文件也会删除其 `## Purpose`；存在其他章节则阻止退役。报告退役时点明 Purpose。仅当规格位于调用方的检出目录时，给出可直接粘贴的 `git checkout` 恢复命令；否则给出明确限定到对应检出目录的恢复说明。

**RENAMED Requirements：** 找到 FROM 需求，将名称改为 TO。

**增量中的 `## Purpose`：** 已有主规格的 Purpose 为准，保持不动；与 `openspec archive` 一致，提醒后继续。

#### d. 新能力创建主规格

只有增量包含可写入的 ADDED 需求，且步骤 b 未因 MODIFIED 或 RENAMED 阻塞时，才创建。其他情况不创建任何内容，规格目录保持不动。

- 只有 REMOVED 的增量，若 `.openspec.yaml` 声明 `retire_capabilities: true`，报告该能力已经退役，继续且不重建规格；缺少标记则报告同步被阻塞，因为 `openspec archive` 会以 `Spec must have at least one requirement` 拒绝。
- 空增量没有可同步操作，也报告阻塞。绝不写空的 `## Requirements`。
- 创建 `<planningHome.root>/openspec/specs/<capability-path>/spec.md`。
- 添加 Purpose：有增量 `## Purpose` 正文时逐字复制，与 `openspec archive` 一致；没有时才用简短 TBD 占位。
- Requirements 节加入 ADDED 需求。
- 遵循后面的“主规格格式参考”。

### 5. 校验更新后的主规格

运行 `openspec validate --specs`，保留之前选择根目录的参数。校验失败时报告问题，不宣称同步成功。

### 6. 展示总结

说明：

- 更新了哪些能力。
- 新增、修改、删除、重命名了哪些需求。
- 哪些新主规格仍留有 TBD Purpose 占位，推动现在补齐，避免长期遗留。
- 哪些能力已退役：点名删除的 `spec.md` 及其 Purpose，并给出可粘贴的 `git checkout` 或限定检出目录的恢复说明。

## 增量规格格式参考

以下结构关键字必须保留英文，以供 OpenSpec 解析；内容使用中文。

```markdown
# Spec Delta

## Purpose

仅在增量引入全新能力时提供，作为新主规格的用途说明。

## ADDED Requirements

### Requirement: 新功能
系统 SHALL 提供某项新行为。

#### Scenario: 基本情况
- **WHEN** 用户执行 X
- **THEN** 系统执行 Y

## MODIFIED Requirements

### Requirement: 已有功能
系统 SHALL 继续提供已有行为，并支持 A。

#### Scenario: 主规格已有的场景
- **WHEN** 用户执行 X
- **THEN** 系统执行 Y

#### Scenario: 新增场景
- **WHEN** 用户执行 A
- **THEN** 系统执行 B

## REMOVED Requirements

### Requirement: 已弃用功能

## RENAMED Requirements

- FROM: `### Requirement: 旧名称`
- TO: `### Requirement: 新名称`
```

## 主规格格式参考

主规格是增量合并的目标，绝不包含增量操作标题 `## ADDED Requirements`、`## MODIFIED Requirements`、`## REMOVED Requirements`、`## RENAMED Requirements`。同步后，所有需求位于单一 `## Requirements` 下。

```markdown
# <capability> Specification

## Purpose
简要说明此能力做什么、为何存在。

## Requirements

### Requirement: 新功能
系统 SHALL 提供某项新行为。

#### Scenario: 基本情况
- **WHEN** 用户执行 X
- **THEN** 系统执行 Y
```

## 核心原则：智能合并

进行合并而非覆盖：

- MODIFIED 块承载完整需求，包括正文及变更后保留的所有场景。遗漏主规格中仍存在的场景时，`openspec validate` 和 `openspec archive` 都会拒绝。
- 保留增量未提及的所有内容，并维持主规格既有顺序。
- 结合语义判断，合理合并变化。

## 成功输出示例

```text
## 规格已同步：<change-name>

已更新主规格：

<capability-1>：
- 新增需求：“新功能”
- 修改需求：“已有功能”（增加 1 个场景）

<capability-2>：
- 创建主规格文件
- 新增需求：“另一项功能”

主规格已更新，变更仍保持活动状态，实施完成后再归档。
```

## 约束

- 修改前同时读取增量和主规格。
- 保留增量未提及的已有内容。
- 不把增量文件原样复制为主规格；合并内容并保持主规格格式，不留增量操作标题。
- 不清楚时询问；执行中展示改动。
- 保持幂等，重复运行应得到相同结果。
- 仅使用 `artifactPaths.specs.existingOutputPaths`，不从无关产物推测增量。
- 尊重调用方指定的子集，不扩大回全部列表。
- 直接同步只获取一次规格指令；归档行内调用则复用其快照。
- 规格指令非零退出或 JSON 无效时，在任何主规格写入前停止。
- 产物规则只约束正在写入的规格，不复制进输出文件。
