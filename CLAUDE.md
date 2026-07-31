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

# Run the app (needs PAPERFLOW_API_KEY for DeepSeek)
PAPERFLOW_API_KEY=sk-xxx conda run -n paperflow python -m paperflow
```

Always use `conda run -n paperflow` — never bare `python` or `pip`.

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
