# agents/

## Scope

- 本文件覆盖 `agents/` Agent 插件目录；每个子目录是一个可被 AgentRegistry 发现的 agent。

## Directory Structure

```text
agents/
├─ supervisor/   # 调度主管：拆解任务、spawn、汇总
├─ searcher/     # 多源搜索 → reviewer 门禁 → 可选下载
├─ noter/        # 纯笔记生成，内部 reviewer 审稿 ≤3 轮
├─ researcher/   # 选题发现：语料盘点 → survey/gaps → idea 卡 → 研究计划
├─ librarian/    # 文献库维护：references.bib 同步/新增/删除/查询导出
├─ reviewer/     # 叶子审稿：笔记审稿 / 下载门禁 / plan_review 三种模式
└─ qa-agent/     # 论文/笔记/阅读记忆问答
```

每个插件恰好两个文件：

- `AGENT.md` — YAML frontmatter（`name`/`description`/`allowed_agents`/`allowed_spawns`）+ Markdown 正文
- `tools.py` — module-level `TOOLS: list[Tool]`（Tool ABC 子类：`name`/`description`/`parameters` JSON Schema/`execute`）

## Core Rules

- **契约式正文**：派发类 worker 用五段式（身份/边界/能力/交付契约/方法启发式）；reviewer/qa-agent 按角色裁剪。编排决策由 LLM 运行时自主，不在 AGENT.md 写跨 agent 编排序列。
- **通用铁律不重复**：诚实性协议/交付契约语义/协作语义在全 agent 共有的 `BASE_PROMPT`（`paperflow/core/agent/base_prompt.py`），AGENT.md 只写角色特有契约。
- **权限最小化**：`allowed_spawns` 由 spawn 工具运行时强制；叶子 agent（reviewer/qa-agent）不递归 spawn；supervisor 之外谁装记忆工具、装哪组，由「谁干活谁记录」原则在装配层决定。
- **frontmatter 是契约**：`name` 必须与目录名一致；`allowed_spawns` 只能引用已存在的 agent。
- 新增 agent：建目录 → 写 AGENT.md + tools.py → 在根 manifest 的 agent 表补一行 → 更新 supervisor 的派发映射（如适用）。

## Investigation Rule

1. 先看目标 agent 的 AGENT.md（契约与启发式），再看 tools.py（实际能力）。
2. 装配差异（谁装哪些工具/回调）查 `paperflow/cli.py`；spawn 门禁查 `paperflow/tools/orchestration/spawn.py`。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 注册与运行时：[`../paperflow/core/AGENTS.md`](../paperflow/core/AGENTS.md)（agent/registry.py）
