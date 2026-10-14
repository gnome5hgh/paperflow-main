# paperflow/.paperflow/skills

## Scope

- 覆盖 `.paperflow/skills/`：**运行时 Skill 的资产根**——给 paperFlow 自己的 agent 注入领域知识、流程指令与轻量工具的能力包，随仓库分发。
- 与编码代理用的 skill 无关（那是 `.agents/skills/`，见文末 Routing）：两条线互不相干，别混。

## Module Positioning

- 角色：`asset-root-manifest-dir`
- 职责：以「一份 skill 一个目录」承接流程与模板——把「怎么做」从 agent 契约里沉下来，做成可加载、可校验、可安装的资产。

## Directory Structure

- `<skill-name>/SKILL.md` — 入口，agentskills.io 格式：frontmatter（名字 + 描述）+ 正文
- `<skill-name>/references/` — 可选，L3 资源（模板、口径等按需读取）
- `<skill-name>/tools.py` — 可选，模块级工具列表（当前没有 skill 携带）

现有六份流程 skill：写笔记、写选题计划两份写作流程；笔记审查、选题产物审查、下载门禁三份审查流程；以及引用格式。

## Responsibilities

- 承载流程步骤与产物标准（模板），让对应角色的 `AGENT.md` 只写契约并指向它。
- 保证每份 skill 结构与命名符合规范，能被注册表校验通过。
- 让审查方自包含地读到验收标准，而不必跨 skill 借写作流程的资源。

## Core Logic

- **规范强制项**：frontmatter 必填名字，且**必须与目录名一致**，违反即注册报错（fail-closed）。
- **三级渐进披露**落在目录结构上：L1 名字 + 描述进 head、L2 正文按需加载、L3 读 `references/`（路径围栏限本 skill 目录内）。
- **模板是分发物，不是快照**：产物标准以 skill 资源为准，代码里不另存一份。
- **写作方与审查方各持一份模板副本**：审查 skill 里放一份同样的模板，让审查方自洽。**两份内容一致靠约定与人工同步，代码层不做校验**——改模板必须同时改两处。

## Key Entry Points

- `write-note/SKILL.md` — 笔记流程（含笔记模板）
- `write-research-plan/SKILL.md` — 选题与计划流程（含四份选题模板）
- `review-note/`、`review-plan/`、`review-download/` — 三类审查流程（审查方持有的模板副本在各自的 `references/`）
- `citation-format/` — 引用格式渲染口径

## Usage And Change Notes

- **改一份模板要改两处**：写作 skill 的那份 + 审查 skill 的副本。只改一处，审查会按旧标准放行或误杀。
- 新增 skill：建目录（目录名 = 将来 frontmatter 里的名字）→ 写 `SKILL.md` → 需要资源时放 `references/`。
- 流程变了就改 skill 正文：**不要把流程写回 `AGENT.md`**（`AGENT.md` 只留契约与指针）。
- 描述要写「谁该在什么时候加载它」——所有 agent 都能看到清单（含 supervisor），领域边界只由描述里的触发语境承担。
- 含 `tools.py` 的 skill 会被并入子 agent 工具表（**不并入 supervisor**），因此写工具时要按 `core/tool/` 的契约声明安全元数据。

## Dependencies And Collaboration

- 被 `paperflow/core/skills/`（发现、校验、加载、工具并入）装载；被各 `agents/<name>/AGENT.md` 指向。
- 目录本身是运行时能力包，与 `.paperflow/` 下的运行期数据（记忆、审计、索引状态）在同一条根下但性质不同：**这里入库，那里不入库**。

## Investigation Rule

1. 「某个角色该怎么做」先看它指向的 skill，而不是 `AGENT.md`；模板在 `references/` 下。
2. skill 不生效先看注册期的 fail-closed 报错（通常名字与目录名不一致或缺字段）。
3. 审查判定与写作标准打架时，比对两边的模板副本——这正是它们会漂的地方。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core -k "skill" -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core -k "skill" -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（.paperflow manifest）
- 装载与安装：[`../../paperflow/core/skills/AGENTS.md`](../../paperflow/core/skills/AGENTS.md)
- 角色契约：[`../../agents/AGENTS.md`](../../agents/AGENTS.md)

## References

- [`write-note/SKILL.md`](write-note/SKILL.md)
- [`write-research-plan/SKILL.md`](write-research-plan/SKILL.md)
- [`review-note/SKILL.md`](review-note/SKILL.md)
- [`../../paperflow/core/skills/AGENTS.md`](../../paperflow/core/skills/AGENTS.md)
