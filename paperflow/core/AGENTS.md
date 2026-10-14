# paperflow/core/

## Scope

- 本文件覆盖 `core/` 核心运行层；各子包内部实现以源码为准。

## Directory Structure

```text
core/
├─ AGENTS.md       # 本 manifest（与 __init__.py 同处；本层不直接放代码模块）
├─ __init__.py
├─ constants/      # 跨模块词汇：constants/enums/(RiskLevel/SideEffect/Provenance 各一文件) + constants.py(RISK_LEVELS/SIDE_EFFECTS/RISK_ORDER，由枚举派生)
├─ agent/          # ReAct 循环：runtime.py(Agent.run 主循环) + registry.py(AgentRegistry 插件发现) + base_prompt.py(全 agent 共有行为基座) + state.py(会话/运行期状态容器)
│                  #   + domain/dto/AgentEvent 引擎事件 + config/AgentConfig 插件配置 + constants/enums/AgentEventKind
├─ llm/            # 接入层：domain/dto/（messages/ wire 消息家族：基类 + system/user/assistant/tool 四子类
│                  #     + response_meta/Usage 响应信封，自带 dict 往返供落盘）+ config/StructuredOutputConfig
│                  #   + services/(client.py 异步客户端 + structured.py 结构化输出)
├─ security/       # 安全中间件洋葱（有 AGENTS.md）：middleware/(base.py 协议 + audit/workspace 策略/输出扫描/策略引擎)
│                  #   + services/(network.py SSRF 校验 + command.py 命令解析 + classifier.py 语义判定)
│                  #   + constants/enums/(ToolDecision 决策 + ApprovalOutcome 审批 + ToolResultStatus 结果状态)
│                  #   + domain/dto/(ToolContext 调用上下文 + AuditEntry 审计事件)
├─ memory/         # Letta 记忆栈移植：constants/(工具名集合与播种文案；角色词 MessageRole 已归 core/common) + schemas/(含 StoredMessage 持久化消息，meta 即响应信封) + storage/(SQLite；加列走 _ADDITIVE_COLUMNS 增量迁移) + common/(领域异常) + services/(管理器 + compaction.py + consolidation.py)
├─ intent/         # 意图识别（可选预处理层），按角色分层：constants/(enums/IntentType+IntentStep + 类别词汇/KB 路径) +
│                  #   schemas/(产出契约 IntentOutput + 回传 Turn + JevDecision + 知识库 IntentClass/Rule) + rules/(实体抽取 + 知识库装载校验) + services/(判定服务客户端 + 集成缝)
├─ mcp/            # MCP 客户端平台（有 AGENTS.md）：constants/enums/McpConnectionState + domain/dto/(McpToolSpec 工具描述
│                  #   + ServerStatus 观测状态) + services/(client.py 后台循环与持久会话 + bridge.py 逐工具桥接)
├─ skills/         # Skill 插件体系（有 AGENTS.md）：config/SkillConfig + services/(registry 发现校验 +
│                  #   install 准入与 lock + assembly 工具并入)
├─ tool/           # Tool 抽象：base.py(Tool ABC 与安全元数据) + result.py(ToolResult) + constants.py(summary 跨模块键) + validation.py(元数据加载期校验)
├─ common/         # 跨子包共享的叶子能力与取值：frontmatter.py(AGENT.md/SKILL.md 解析) + tokenization.py(token 计数单点)
│                  #   + text.py(未配对代理字符清洗) + constants/(enums/MessageRole 角色词 + 无子包归属的共享取值，如重试退避基数)
├─ rag/            # RAG 检索栈（有 AGENTS.md）：domain/(entity + dto) + constants/(块契约 + enums/IndexOutcomeStatus)
│                  #   + parsers/ 解析分块 + encoders/ 稀疏稠密精排 + services/ 门面与编排 + storage/ Milvus 与图表原图
└─ vision/         # 视觉分析（有 AGENTS.md）：pdffigures2 提取管线（parsers/ 解析 + detectors/ 图检测 + 编排 + 视觉模型看图）
                   #   constants/enums/FigureType + schemas/ + common/ 几何原语 + services/
```

**分层规矩**：本层只放子包（外加本 manifest 与 `__init__.py`），不放裸 `.py` 模块——散在根下的模块按「服务于谁」收进对应子包（跨子包共享的进 `common/`，有明确归属的进那个子包）。**要给某个子包加自己的 manifest，先把它的代码模块收进 `services/` 等子包**，否则 manifest 就与代码同层了（`skills/`、`mcp/`、`security/`、`rag/`、`vision/` 五份就是这么落地的；`agent/`、`constants/`、`tool/`、`common/` 的包根还有模块，但它们没有自己的 manifest，规则不触发）。

**枚举一枚举一文件**：各子包的 `constants/` 里，枚举放 `enums/<snake_case>.py`（一个枚举一个文件），其余常量放同层 `constants.py`；`constants/__init__.py` 只做再导出，消费方一律从 `paperflow.core.<子包>.constants` 取。判据是「有没有第二个消费方」——只有一个消费方的常量就地声明。

**两类 `config/`**：顶层 `paperflow/config/` 是与 config.yaml 同构、可经 env 派生的**全局可调参数**；各子包的 `config/` 放该包**组件自己的行为参数**（调用点各给一套、不进 config.yaml）——`agent/config/AgentConfig`、`skills/config/SkillConfig`、`llm/config/StructuredOutputConfig`。这类对象不是「层间搬运的数据」，故不住 `domain/dto/`。

**`rag` 与 `vision` 是本层的子包**：rag 的媒体块要向 vision 取图提取与视觉描述（`core.rag → core.vision` 是 core 内部边，惰性 import），所以两者必须同层；本层对 `tools`/`terminal`/`citations` 的出边为 0。

## Core Rules

- **ReAct 循环是唯一执行模型**：无确定性 pipeline，路由/工具选择/任务拆解全由 LLM 驱动；工具只是 JSON Schema 定义。
- **安全是中间件洋葱**：before（可拒/要求确认）→ 执行 → 逆序 after；所有拦截降级为 ToolResult 文本（`security_blocked`/`user_denied` 等），绝不抛进 ReAct 循环。
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
- 兄弟子包 manifest：[`rag/AGENTS.md`](rag/AGENTS.md) / [`vision/AGENTS.md`](vision/AGENTS.md) / [`security/AGENTS.md`](security/AGENTS.md) / [`skills/AGENTS.md`](skills/AGENTS.md) / [`mcp/AGENTS.md`](mcp/AGENTS.md)
- 兄弟包工具实现：[`../tools/AGENTS.md`](../tools/AGENTS.md)
