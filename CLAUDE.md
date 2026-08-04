# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dev dependencies (in conda env)
conda run -n paperflow pip install -e ".[dev]"

# Run all tests
conda run -n paperflow python -m pytest tests/ -v

# Run a single test
conda run -n paperflow python -m pytest tests/test_agent.py::TestExecTool -v

# Run the app — 交互式 REPL（⚠️ 不能经 conda run）
# conda run 不转发 stdin 给子进程 → 交互式 REPL 的 input() 立即 EOF 退出。
# 必须先在激活的 env 里跑，或用 env 的 python 直接跑：
conda activate paperflow && python -m paperflow
# 或 /opt/miniconda3/envs/paperflow/bin/python -m paperflow
```

Always use `conda run -n paperflow` for 非交互命令（测试/脚本/安装）——never bare `python` or `pip`。**例外：交互式 REPL（`python -m paperflow`）不能经 `conda run`**——它不转发 stdin，REPL 一启动就 EOF 退出；需 `conda activate paperflow` 后直接 `python -m paperflow`。

## 文档同步规则

修改代码时，必须同步更新关联的设计文档：

- **新增/修改任何代码后，都要先思考是否需要同步更新 ADR / spec / plan**：接口签名、行为语义、结构或已记录决策发生变化都算。需要同步时，在**测试代码通过后**再更新对应文档——先让代码行为被测试锁住，再让文档描述现状
- 如果在实现过程中发现 Layer N 的 spec/plan 文档与最终代码不一致，修改代码后需同步更新对应的 spec（`docs/superpowers/specs/`）和 plan（`docs/superpowers/plans/`）
- 如果当前 Layer 的修改影响了上层 Layer 的 spec/plan（如 Layer 1 实现时发现 Layer 0 的接口需要调整），同样需要回修受影响的上层文档
- ADR（`docs/adr/`）是架构决策记录，一般不应被实现代码反向修改。但如果代码实现揭示出 ADR 设计缺陷（如接口不可行、组件拆分不合理），需在 ADR 中追加修正说明

## Code style

Write detailed comments in all code. Explain the WHY behind non-obvious logic — design constraints, edge cases, workarounds, and architectural intent. Assume future readers (including yourself) have zero context on why a piece of code exists. Comments should be in Chinese.

Good comments explain the reason, not the mechanics:
```python
# BAD: "Loop over items and add to result"
# GOOD: "遍历所有 items 并去重，因为多源搜索结果可能包含同一篇论文的不同版本"
```

Keep comments up to date when modifying code — stale comments are worse than no comments.

## Architecture

paperFlow is an LLM-driven academic research workflow assistant. Architecture reference: ADR 0003.

### Agent plugin system

Every agent lives in `agents/<name>/` with two files:
- `SKILL.md` — YAML frontmatter (`name`, `description`, `skills`, `allowed_agents`, `allowed_spawns`) + Markdown body (used as `system_prompt`)
- `tools.py` — module-level `TOOLS: list[Tool]` list. Each Tool is a subclass of `Tool` ABC with `name`, `description`, `parameters` (JSON Schema for OpenAI function calling), and `execute(**kwargs) -> ToolResult`

`AgentRegistry(agents_dir)` scans this directory at init time, parses frontmatter, dynamically imports `TOOLS` from each `tools.py`, and exposes `get_config(agent_type) -> AgentConfig` plus `list_agents()`. It is the single entry point for agent discovery — no separate tool/skill registries.

### Agent and ReAct loop

`Agent(llm, agent_registry, agent_type)` uses pull mode — it calls `agent_registry.get_config(agent_type)` internally to load tools and system prompt.

`Agent.run(task) -> str` is an async ReAct loop:
1. Send `[system_prompt, task]` to LLM with tool schemas
2. If response has no `tool_calls` → return `content` (done)
3. If response has `tool_calls` → execute each tool via `_exec_tool()`, append tool results as `role="tool"` messages, loop
4. If loop exceeds `max_turns` (default 20) → raise `MaxTurnsExceeded`

`_exec_tool` handles three error paths gracefully (returns error `ToolResult` instead of raising): JSON parse errors on arguments, unknown tool names, and tool execution exceptions.

### LLM client

`LLMClient` wraps the `openai` SDK as an async client via `asyncio.to_thread`. `Message` is a simple dataclass (`role`, `content`, `tool_calls`, `tool_call_id`) that serializes to OpenAI wire format. `tool_to_openai_schema(t: Tool) -> dict` converts a Tool to the OpenAI function-calling JSON Schema format.

### Config

`PaperFlowConfig.from_env()` loads in priority order: environment variables (`PAPERFLOW_API_KEY`, `PAPERFLOW_BASE_URL`, `PAPERFLOW_MODEL`, `PAPERFLOW_WORKSPACE`, `PAPERFLOW_AGENTS_DIR`) > `config.yaml` > dataclass defaults (DeepSeek endpoint, `deepseek-chat` model).

### Key design decisions (Layer 0)

- **No deterministic pipeline.** Everything — routing, tool selection, task decomposition — is driven by the LLM's ReAct loop. Tools are just JSON Schema definitions fed to the model.
- **`ToolResult.summary: dict`** exists from day one (default empty) as a forward hook for the memory system (Layer 1+).
- **`risk_level`** on Tool ABC is declared but not enforced yet — Policy Engine arrives in Layer 1.
- **`allowed_agents` and `allowed_spawns`** on AgentConfig are parsed but not enforced yet — spawn permissions arrive with the Supervisor in later layers.
- **`Agent.run()` returns plain `str`** in Layer 0. Structured `SubAgentResult` comes with Supervisor spawn logic (Layer 4).
