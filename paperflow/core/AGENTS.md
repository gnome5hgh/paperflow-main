# paperflow/core/

## Scope

- 本文件覆盖 `core/` 核心运行层；各子包内部实现以源码为准。

## Directory Structure

```text
core/
├─ AGENTS.md       # 本 manifest（与 __init__.py 同处；本层不直接放代码模块）
├─ __init__.py
├─ constants/      # 跨模块词汇：RiskLevel/SideEffect 枚举 + RISK_LEVELS/SIDE_EFFECTS/RISK_ORDER（由枚举派生）
├─ agent/          # ReAct 循环：runtime.py(Agent.run 主循环) + registry.py(AgentRegistry 插件发现) + base_prompt.py(全 agent 共有行为基座) + state.py(会话/运行期状态容器)
│                  #   + domain/dto/(AgentConfig 插件配置 + StreamEvent 流式事件)
├─ llm/            # 接入层：domain/dto/message.py(wire 消息契约) + services/(client.py 异步客户端 + structured.py 结构化输出)
├─ security/       # 安全中间件洋葱（有 AGENTS.md）：middleware/(base.py 协议 + audit/workspace 策略/输出扫描/策略引擎)
│                  #   + services/network.py(SSRF 校验) + domain/dto/(ToolContext 调用上下文 + AuditEntry 审计事件)
├─ memory/         # Letta 记忆栈移植：constants/(MessageRole 枚举 + 工具常量) + schemas/ + storage/(SQLite) + common/(领域异常) + services/(管理器 + compaction.py + consolidation.py)
├─ intent/         # 意图识别（可选预处理层），按角色分层：constants/(枚举 + 类别词汇/KB 路径) +
│                  #   schemas/(产出契约) + rules/(实体抽取 + 知识库装载校验) + services/(判定服务客户端 + 集成缝)
├─ mcp/            # MCP 客户端平台（有 AGENTS.md）：constants/(连接状态枚举) + domain/dto/(McpToolSpec 工具描述
│                  #   + ServerStatus 观测状态) + services/(client.py 后台循环与持久会话 + bridge.py 逐工具桥接)
├─ skills/         # Skill 插件体系（有 AGENTS.md）：domain/dto/SkillConfig + services/(registry 发现校验 +
│                  #   install 准入与 lock + assembly 工具并入)
├─ tool/           # Tool 抽象：base.py(Tool ABC 与安全元数据) + result.py(ToolResult) + validation.py(元数据加载期校验)
└─ common/         # 跨子包共享的叶子能力：frontmatter.py(AGENT.md/SKILL.md 解析) + tokenization.py(token 计数单点)
                   #   + text.py(未配对代理字符清洗)
```

**分层规矩**：本层只放子包（外加本 manifest 与 `__init__.py`），不放裸 `.py` 模块——散在根下的模块按「服务于谁」收进对应子包（跨子包共享的进 `common/`，有明确归属的进那个子包）。**要给某个子包加自己的 manifest，先把它的代码模块收进 `services/` 等子包**，否则 manifest 就与代码同层了（`skills/`、`mcp/`、`security/` 三份就是这么落地的；`agent/`、`constants/`、`tool/`、`common/` 的包根还有模块，但它们没有自己的 manifest，规则不触发）。

## Core Rules

- **ReAct 循环是唯一执行模型**：无确定性 pipeline，路由/工具选择/任务拆解全由 LLM 驱动；工具只是 JSON Schema 定义。
- **安全是中间件洋葱**：before（可拒/要求确认）→ 执行 → 逆序 after；所有拦截降级为 ToolResult 文本（`policy_denied`/`user_denied` 等），绝不抛进 ReAct 循环。
- **SQL 是记忆真相源，markdown 是投影**：压缩/窗口驱逐永不删 SQL 行；记忆块写入是一次持锁的原子读-改-写 + 写入 CAS（读-改-写类写经 `mutate_block`），带历史快照。
- **运行期状态收在两个容器**：`agent/state.py` 的 `SessionState`（跨 run：只剩连续失败计数）与 `RunState`（按 trace 隔离：搜索去重池、在途派发去重注册表、产物账本、各预算计数、在途写占用——同路径写互斥的判定已下沉到 runtime 执行层，容器只存占用），按 TTL 惰性清扫（run 整份丢弃、session 逐条过期），不再散落模块级字典。
- **意图只进根 agent**：spawn 的子 agent 不装配意图服务（`intent_service=None`），故不做意图识别。
- **流式零开销**：`stream_callback`/`telemetry_callback` 为 None 时全链路保持非流式行为。

## Key Entry Points

- `agent/runtime.py` — `Agent.run()` async ReAct 循环（head 构造/压缩检查/工具并发/截断续写）
- `agent/registry.py` — Agent 插件唯一发现入口（扫 `agents/` 目录）
- `security/base.py` — `SecurityMiddleware` ABC + 异常体系（`ToolContext`/`AuditEntry` 在 `security/domain/`）
- `skills/registry.py` — `SkillRegistry`（扫 `.paperflow/skills/`，name 必须与目录名一致）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 兄弟包工具实现：[`../tools/AGENTS.md`](../tools/AGENTS.md)
