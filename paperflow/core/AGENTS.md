# paperflow/core/

## Scope

- 本文件覆盖 `core/` 核心运行层；各子包内部实现以源码为准。

## Directory Structure

```text
core/
├─ agent/          # ReAct 循环：runtime.py(Agent.run 主循环) + registry.py(AgentRegistry 插件发现) + base_prompt.py(全 agent 共有行为基座)
├─ llm/            # LLMClient(openai SDK 异步封装：chat/chat_stream/参数降级重试) + embedding.py / rerank.py(云端协议与实现同文件)
├─ security/       # 安全中间件洋葱：base.py 协议 + middleware/(audit/workspace 策略/输出扫描/策略引擎) + network.py + text.py
├─ memory/         # Letta 记忆栈移植：schemas/ + orm/(SQLite) + services/ + tools/ + compaction.py + sleeptime.py
├─ intent/         # 意图识别五级级联：entities → option_reply → followup → HybridRouter → LLM 兜底
├─ structured/     # 结构化输出（pydantic schema 契约抽取）
├─ mcp/            # MCP 客户端平台：后台事件循环 + 逐工具桥接
├─ skills/         # SkillRegistry：frontmatter 校验 + tools.py 动态导入 + 资源围栏
├─ tool.py         # Tool ABC 与安全元数据（risk_level/side_effects/requires_confirm/…）
├─ frontmatter.py  # AGENT.md/SKILL.md frontmatter 解析
└─ tokenization.py # token 估算
```

## Core Rules

- **ReAct 循环是唯一执行模型**：无确定性 pipeline，路由/工具选择/任务拆解全由 LLM 驱动；工具只是 JSON Schema 定义。
- **安全是中间件洋葱**：before（可拒/要求确认）→ 执行 → 逆序 after；所有拦截降级为 ToolResult 文本（`policy_denied`/`user_denied` 等），绝不抛进 ReAct 循环。
- **SQL 是记忆真相源，markdown 是投影**：压缩/窗口驱逐永不删 SQL 行；记忆块写入是一次持锁的原子读-改-写 + 写入 CAS（读-改-写类写经 `mutate_block`），带历史快照。
- **运行期状态收在两个容器**：`agent/state.py` 的 `SessionState`（跨 run：只剩连续失败计数）与 `RunState`（按 trace 隔离：搜索去重池、在途派发去重注册表、派发与产物账本、各预算计数），按 TTL 惰性清扫（run 整份丢弃、session 逐条过期），不再散落模块级字典。
- **意图只进根 agent**：spawn 的子 agent 不传意图管线（门控关闭，省 LLM 调用）。
- **流式零开销**：`stream_callback`/`telemetry_callback` 为 None 时全链路保持非流式行为。

## Key Entry Points

- `agent/runtime.py` — `Agent.run()` async ReAct 循环（head 构造/压缩检查/工具并发/截断续写）
- `agent/registry.py` — Agent 插件唯一发现入口（扫 `agents/` 目录）
- `security/base.py` — `ToolContext` + `SecurityMiddleware` ABC + 异常体系
- `skills/registry.py` — `SkillRegistry`（扫 `.paperflow/skills/`，name 必须与目录名一致）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 兄弟包工具实现：[`../tools/AGENTS.md`](../tools/AGENTS.md)
