# agents/

## Scope

- 本文件覆盖 `agents/` Agent 插件目录；每个子目录是一个可被 AgentRegistry 发现的 agent。

## Directory Structure

```text
agents/
├─ supervisor/       # 调度主管：拆解任务、spawn、汇总；能自答的先自答
├─ paper-agent/      # 论文域责任人：多源搜索 → review-agent 门禁 → 可选下载；读 PDF 与图表
├─ note-agent/       # 笔记域责任人：基于指定 PDF 起草结构化笔记，内部 spawn review-agent 审稿
├─ research-agent/   # 选题域责任人：语料盘点 → survey/gaps → idea 卡 → 研究计划
├─ review-agent/     # 审查域责任人：笔记审查 / 下载门禁 / 选题产物审查（开审前加载对应流程 skill）
├─ citation-agent/   # 引用域责任人：references.bib 同步/新增/删除/查询导出
├─ rag-agent/        # 语料索引责任人：检索 + 入库 + 删除后全量收敛
└─ memory-agent/     # 记忆域责任人：11 件记忆工具全装，其余 agent 一件不装
```

命名一律 `<域>-agent`；`supervisor` 是唯一的例外（它是编排层，不是领域责任人）。

每个插件恰好两个文件：

- `AGENT.md` — YAML frontmatter（`name`/`description`/`allowed_spawns`）+ Markdown 正文
- `tools.py` — module-level `TOOLS: list[Tool]`（Tool ABC 子类：`name`/`description`/`parameters` JSON Schema/`execute`）

## Core Rules

- **判据是「一个 agent = 一类产物的责任人」**：每个领域角色独占自己那类产物的读写（引用库归 `citation-agent`、索引归 `rag-agent`、记忆归 `memory-agent`），其余角色需要时**派发**它而不是自己装工具。`supervisor` 是刻意的例外——它只编排，不持有任何执行类工具。
- **契约式正文**：派发类 worker 用五段式（身份/边界/能力/交付契约/方法启发式）；`review-agent` 按角色裁剪，读的是三份审查流程 skill。编排决策由 LLM 运行时自主，不在 AGENT.md 写跨 agent 编排序列。
- **流程写在 skill 里**：写笔记 / 写选题计划 / 三类审查的步骤沉在 `.paperflow/skills/` 的五份流程 skill 里，AGENT.md 只写契约与启发式并指向它（`load_skill`）。模板等产物标准是 skill 的资源，随流程分发。
- **通用铁律不重复**：诚实性协议/交付契约语义/协作语义在全 agent 共有的 `BASE_PROMPT`（`paperflow/core/agent/base_prompt.py`），AGENT.md 只写角色特有契约。
- **权限最小化**：`allowed_spawns` 由 spawn 工具运行时强制；叶子 agent（`citation-agent`/`rag-agent`/`memory-agent`）不递归 spawn；`supervisor` 不装 skill 工具（代码级红线）。
- **frontmatter 是契约**：`name` 必须与目录名一致；`allowed_spawns` 只能引用已存在的 agent。
- 新增 agent：建目录 → 写 AGENT.md + tools.py → 在根 manifest 的 agent 表补一行。`description` 会被自动收进 supervisor 的 system 消息里的 `<available_agents>` 清单，supervisor 按能力选型——没有「意图 → 子 agent」映射表要维护。

## Investigation Rule

1. 先看目标 agent 的 AGENT.md（契约与启发式），再看 tools.py（实际能力）。
2. 装配差异（谁装哪些工具/回调）查 `paperflow/cli.py`；spawn 门禁查 `paperflow/tools/orchestration/spawn.py`。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 注册与运行时：[`../paperflow/core/AGENTS.md`](../paperflow/core/AGENTS.md)（agent/registry.py）
