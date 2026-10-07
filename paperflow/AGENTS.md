# paperflow/

## Scope

- 本文件覆盖 `paperflow/` 主包；进入下层子包后，优先服从更近的 `AGENTS.md`（`core/`、`rag/`、`tools/`、`citations/`、`vision/`、`terminal/` 各有一份）。
- 包级事实以源码与 `tests/` 为准，本文件只做导航与约束。

## Module Positioning

- 角色：`package-root-manifest`
- 主包唯一入口，`paperflow.cli` 是装配根：构造 config → 安全中间件链 → 记忆服务 → 意图管线 → Agent 装配 → REPL。

## Directory Structure

```text
paperflow/
├─ cli.py          # 装配根：中间件/记忆/意图/Agent 全部在此接线
├─ config.py       # 全部可调参数的唯一声明点（dataclass 树 + 递归合并 + env 派生）
├─ core/           # 核心运行层（有 AGENTS.md）
├─ rag/            # RAG 检索栈（有 AGENTS.md）
├─ citations/      # 引用管理（有 AGENTS.md）：bib.py / corpus.py / manager.py，懒加载单例
├─ vision/         # 视觉分析（有 AGENTS.md）：pdffigures2 提取管线 + 视觉模型看图
├─ tools/          # 原子工具（有 AGENTS.md）
└─ terminal/       # 终端交互隔离层（有 AGENTS.md）：repl/ io/ render/ 确认中心/ 斜杠命令
```

## Core Rules

- **config.py 是唯一声明点**：任何可调参数先在 dataclass 树加字段（含 YAML 覆盖与 env 派生），消费模块直接读 config，不在模块内另设常量副本。
- **懒加载单例**：重量组件（RAGService、CitationManager、记忆工具）用双重检查加锁的模块级单例，包导入期不拉重型依赖。
- **依赖方向**：terminal/tools 是被消费方；core 依赖 tools 抽象但不反向；rag/citations/vision 经门面（RAGService / get_citation_manager）对外，内部结构不外泄。
- **错误降级为文本**：工具与中间件层的错误转 ToolResult 文本回传 LLM，只有 `MaxTurnsExceeded` 向上抛。

## Investigation Rule

1. 先读本文件定位子包，再进对应子包 `AGENTS.md`。
2. 装配问题从 `cli.py` 读起（顺序即依赖方向）；参数问题查 `config.py` 与根 manifest 的配置字段表。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 下级：[`core/AGENTS.md`](core/AGENTS.md) / [`rag/AGENTS.md`](rag/AGENTS.md) / [`tools/AGENTS.md`](tools/AGENTS.md) / [`citations/AGENTS.md`](citations/AGENTS.md) / [`vision/AGENTS.md`](vision/AGENTS.md) / [`terminal/AGENTS.md`](terminal/AGENTS.md)
