# paperflow/core/agent

## Scope

- 覆盖 `core/agent/`：ReAct 引擎本体——主循环、插件发现、运行期状态、全 agent 共有的行为基座、工具执行管道与 in-context 窗口。
- 根 manifest 与 `../AGENTS.md` 的硬红线在本层同样成立，这里只写本包特有的。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：跑一轮 ReAct（构造 head → 调模型 → 执行工具 → 收敛），并给所有 agent 提供同一套执行语义。

## Directory Structure

```text
agent/
├─ services/     # 运行时协作者：runtime.py（Agent 主循环）+ registry.py（插件发现）
│                #   + state.py（运行期状态容器）+ context_window.py + tool_executor.py
├─ constants/    # base_prompt.py（BASE_PROMPT 行为基座）+ enums/（引擎事件类型）
├─ config/       # AgentConfig：插件配置（组件自有参数）
└─ domain/dto/   # 引擎事件（AgentEvent）
```

## Responsibilities

- 定义并执行 ReAct 循环，把工具调用、错误降级、结果回填串成一条闭环。
- 保持会话的 in-context 窗口，并在超限时触发压缩。
- 发现 agent 插件并加载其工具与系统提示词。
- 唯一持有跨调用状态的包（两个容器都在这里）。

## Core Logic

- **ReAct 是唯一执行模型**：没有确定性 pipeline——路由、工具选择、任务拆解全由模型决定，工具只是喂给它的 JSON Schema 定义。
- **依赖全走构造参数注入**：安全中间件、确认回调、意图服务、会话 id、记忆服务句柄、流式回调。`None` 一律表示「这一路关掉」，对应代码路径必须零开销跳过——**不要在实现里另读全局配置绕过注入**。
- **运行期状态只放两个容器**（`services/state.py`）：`SessionState` 跨 run 存活，`RunState` 按一次任务 / `trace_id` 隔离。**不要新增模块级字典**攒跨调用状态；两者都按 TTL 惰性清扫（取用时顺手剔除过期项，不起定时任务）。
- **工具调用并发执行，但结果按调用顺序回填**：并发不等于乱序，模型看到的工具结果必须与它发出的调用次序一致。并发度有上限，确认是串行的（确认框非线程安全）。
- **截断必须续写**：模型因长度被截断时，把半截内容与续写提示放回 in-context 继续循环，不能把那半截当最终回答交付。
- **错误一律降级为文本**：工具与中间件层的错误转成结果文本回传模型；唯一向上抛的异常是回合数超限（模型陷入无法自主退出的循环，必须由调用方介入）。
- **每轮开头重建记忆块**：同轮里改过的记忆，下一轮模型调用就能看见——改 head 构造顺序时别破坏这个时机。

## Key Entry Points

- `services/runtime.py` — `Agent.run()`：主循环
- `services/registry.py` — 插件发现与配置加载
- `services/tool_executor.py` — 工具执行管道（中间件洋葱、确认、异常降级）
- `services/context_window.py` — in-context 窗口与压缩触发
- `services/state.py` — 两个状态容器
- `constants/base_prompt.py` — 行为基座（诚实性协议、交付契约语义、协作语义）

## Usage And Change Notes

- **改 head 构造顺序会同时动多种行为**（意图注入、记忆块、可派发清单、技能清单）——顺序是有意的，改动要说明理由。
- **新增构造参数要保持 `None` = 关的语义**，并让相关路径零开销跳过；调用方（装配层与派发工具）要同时更新。
- **派发出去的子 agent 继承哪些、不继承哪些是刻意的**：继承安全中间件、`session_id`、确认回调、技能注册表与工具结果外置回调；**不传**意图管线与会话历史（子任务是结构化任务，不是用户意图）。子 agent 各自持有独立 `conversation_id`（消息不污染父窗口），但共享父 `session_id`（会话内可检索）。改动这个继承面等于改子 agent 的能力与审计归属。
- **任务锚点是请求级注入**：首次压缩后每轮从 `AgentState.task_anchor` 渲染，紧挨当前 user task 之前；**不放进 `window.messages`**，否则会被下一次压缩驱逐。锚点只由压缩链路写，别在别处改。
- **工具结果外置在 after 钩子之后**：`ToolExecutor` 看到完整结果并跑完审计后才交给 `offloader`；offloader 失败时保留原结果，不阻断工具调用。
- 新增状态项先问「它属于会话还是属于一次任务」，再决定放哪个容器，别顺手加模块级变量。

## Dependencies And Collaboration

- 依赖 `core/llm`（每次模型调用）、`core/tool`（工具抽象）、`core/security`（中间件）、`core/memory`（窗口与记忆块）、`core/skills`（技能清单）、`core/intent`（根 agent 的预处理层）。
- 被 `paperflow/cli/` 装配、被 `terminal/` 驱动、被 `tools/orchestration/` 用来构造子 agent。
- 本包**不**依赖 `tools/` 的具体工具与 `terminal/` 的实现——它们通过注入与抽象协作。

## Investigation Rule

1. 主循环行为（轮次、head、压缩、续写）看 `services/runtime.py`；工具执行（中间件、确认、异常）看 `services/tool_executor.py`。
2. 窗口与压缩看 `services/context_window.py`；跨调用状态看 `services/state.py`；插件加载看 `services/registry.py`；通用行为措辞看 `constants/base_prompt.py`。
3. 排查「子 agent 拿不到某能力」先看继承面与装配层，再看插件自己的 `AGENT.md`。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/agent tests/core -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/agent tests/core -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 设计依据：ADR 0003（ReAct 多 Agent 架构）
- 插件契约与角色：[`../../../agents/AGENTS.md`](../../../agents/AGENTS.md)
- 工具抽象：[`../tool/AGENTS.md`](../tool/AGENTS.md)

## References

- [`services/runtime.py`](services/runtime.py)
- [`services/state.py`](services/state.py)
- [`constants/base_prompt.py`](constants/base_prompt.py)
- [`../../../agents/AGENTS.md`](../../../agents/AGENTS.md)
