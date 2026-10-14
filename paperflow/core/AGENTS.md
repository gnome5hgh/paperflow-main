# paperflow/core

## Scope

- 覆盖 `core/` 核心运行层。子包内部实现以源码为准。
- 十二个子包各有自己的 manifest（见 Routing）；根 manifest 的硬红线在本层同样成立，这里不重复。

## Module Positioning

- 角色：`aggregator-manifest-dir`
- 职责：集中放运行时能力——ReAct 引擎、LLM 接入、记忆、意图、安全、RAG、视觉、Skill、MCP 与它们共享的契约；本层只负责聚合与分层约束，具体行为由各子包自己定义。

## Directory Structure

- `agent/` — ReAct 引擎：`services/`（主循环、插件发现、状态容器、执行管道）+ `constants/`（行为基座）
- `llm/` — LLM 接入层：消息家族、客户端、结构化输出
- `memory/` — 记忆栈：持久化、记忆块、压缩与整合
- `intent/` — 意图识别（可选预处理层）
- `security/` — 安全中间件与防护能力
- `skills/` — Skill 插件体系
- `mcp/` — MCP 客户端平台
- `rag/` — RAG 检索栈
- `vision/` — 视觉分析
- `tool/` — Tool 契约：`domain/`（entity 契约 + dto 结果载体）+ `services/`（加载期校验）+ `constants/`
- `common/` — 跨子包共享的叶子能力与取值：`services/`（三个纯函数叶子）+ `constants/`
- `constants/` — 跨模块词汇：`enums/`（风险等级、副作用、来源）+ `derived.py`（派生表）

## Responsibilities

- 定义并维护运行层的分层边界：谁是门面、谁能被谁依赖、状态放在哪里。
- 承载全部与模型交互、检索、记忆、安全保障有关的实现。
- 保证本层对上层（`tools` / `terminal` / `citations`）的出边为 0。

## Core Logic

- **只放子包**，不放裸 `.py` 模块（外加本 manifest 与 `__init__.py`）。散在根下的模块按「服务于谁」收进子包，跨子包共享的叶子进 `common/`。
- **枚举一枚举一文件**：各子包的 `constants/` 里，枚举放 `enums/<snake_case>.py`，其余常量放同层 `constants.py`；`constants/__init__.py` 只做再导出。判据是「有没有第二个消费方」。
- **两类 `config/`**：顶层 `paperflow/config/` 是可经 config.yaml 与 env 调整的全局参数；子包的 `config/` 放该包**组件自己的行为参数**（调用点各给一套、不进 config.yaml）。后者不是层间搬运的数据，故不住 `domain/dto/`。
- **`rag` 与 `vision` 必须同层**：rag 的媒体块要向 vision 取图与视觉描述（core 内部的惰性 import），分开摆会让包级依赖成环。
- **子包内部按角色分层**：门面（对外只经一个单例）、`domain/`（公共词汇）、`services/`（编排）、`storage/`（持久化）、`config/`（组件参数）、`constants/`（本包词汇）——新增子包照这套分。

## Key Entry Points

- `agent/services/runtime.py` — ReAct 主循环
- `security/middleware/base.py` — 安全中间件协议与异常体系
- `rag/services/rag_service.py` — RAG 门面
- `tool/domain/entity/tool.py` — Tool 抽象与安全元数据

## Usage And Change Notes

- **包根只留子包与 manifest**：本层 12 个子包已全部收净（包根无裸 `.py`）。判据是「这个单元有没有自成体系的约定」——真正要避免的是为只有一两个内聚模块、没有独立约定的目录单设一份 manifest。
- 新增或搬移模块时按本层既有角色选子包（`domain/` / `services/` / `storage/` / `config/` / `constants/`），别把散模块直接堆在包根；搬完把包内 import 改成具体子模块路径（不可经包 `__init__`，那会成环）。
- 子包之间新增依赖要检查方向：**core 内部允许互相依赖，但对 `tools` / `terminal` / `citations` 的出边必须为零**。

## Dependencies And Collaboration

- 本层被 `cli/`（装配）、`tools/`（工具实现）、`terminal/`（REPL 与渲染）依赖；本层不反向依赖它们。
- 子包之间的主要依赖：`agent` → `llm` / `tool` / `skills` / `memory` / `security` / `intent`；`rag` → `vision`；`security` → `common` 与 `constants`；`intent` 与 `memory` 互相独立。
- 与各 agent 通过 `Tool` 抽象协作，不是硬编码调用链。

## Investigation Rule

1. 先读本文件确认目标子包与它是否有自己的 manifest。
2. 引擎与工具执行看 `agent/`；模型接入看 `llm/`；记忆看 `memory/`；意图看 `intent/`；安全看 `security/`；检索看 `rag/`；图表看 `vision/`；插件与外部工具看 `skills/` 与 `mcp/`。
3. 共享叶子能力在 `common/`，跨模块词汇在 `constants/`，工具契约在 `tool/`——排查「同一逻辑有两份实现」时先看这三处。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core tests/agent -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core tests/agent -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 子包 manifest：[`agent/`](agent/AGENTS.md) / [`llm/`](llm/AGENTS.md) / [`memory/`](memory/AGENTS.md) / [`intent/`](intent/AGENTS.md) / [`rag/`](rag/AGENTS.md) / [`vision/`](vision/AGENTS.md) / [`security/`](security/AGENTS.md) / [`skills/`](skills/AGENTS.md) / [`mcp/`](mcp/AGENTS.md) / [`tool/`](tool/AGENTS.md) / [`common/`](common/AGENTS.md) / [`constants/`](constants/AGENTS.md)
- 工具实现（面向 agent 的那一层）：[`../tools/AGENTS.md`](../tools/AGENTS.md)

## References

- [`agent/services/runtime.py`](agent/services/runtime.py)
- [`agent/services/state.py`](agent/services/state.py)
- [`tool/domain/entity/tool.py`](tool/domain/entity/tool.py)
- [`../../agents/AGENTS.md`](../../agents/AGENTS.md)
