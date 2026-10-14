# paperflow/

## Scope

- 本文件覆盖 `paperflow/` 主包，并**兼管 `cli/` 与 `config/`**——它们各自只有两个内聚模块，再套一层子包只为放 manifest 是噪音，故不单独设 manifest，其规则见下。
- 进入下层子包后优先服从更近的 `AGENTS.md`（`core/`〔含 `core/rag/`、`core/vision/`、`core/skills/`、`core/mcp/`、`core/security/` 五份〕、`tools/`、`citations/`、`terminal/` 各有一份）。
- 包级事实以源码与 `tests/` 为准，本文件只做导航与约束。

## Module Positioning

- 角色：`package-root-manifest`
- 主包唯一入口，`paperflow.cli` 是装配根：构造 config → 安全中间件链 → 记忆服务 → 意图管线 → Agent 装配 → REPL。

## Directory Structure

```text
paperflow/
├─ __init__.py     # 空（包的标记）
├─ __main__.py     # `python -m paperflow` 的结构性入口（5 行，转调 paperflow.cli.main）
├─ cli/            # 装配根：bootstrap.py(依赖服务预检) + assembly.py(对象图组装)
├─ config/         # 全局可调参数的唯一声明点：sections.py(dataclass 树) + loader.py(合并/env 派生)
├─ core/           # 核心运行层（有 AGENTS.md）：含 rag/ 与 vision/
├─ citations/      # 引用管理（有 AGENTS.md）：constants/ schemas/ storage/ services/，懒加载单例
├─ tools/          # 原子工具（有 AGENTS.md）
└─ terminal/       # 终端交互隔离层（有 AGENTS.md）：repl/ io/ render/ confirm/ commands/ common/
```

**包根只留 `__init__.py` 与 `__main__.py`**：前者是包标记，后者是 `python -m` 要求的结构性入口（移不出去）；其余每个关注点（装配、配置）都成子包，子包 `__init__` 只做再导出。这样包根的 `AGENTS.md` 不与代码模块同层。

**manifest 放在与被覆盖单元同层**——该层若还有代码模块，要么把代码收进子包（`core/skills|mcp|security` 就是这么做的），要么把 manifest 提到上一层（`cli/` 与 `config/` 属于后者：各自只有两个内聚模块，再套一层纯属噪音）。

### cli/ 与 config/ 的要点

- **cli/**：`bootstrap.py` 是依赖服务预检（软依赖语义：任何失败只产警告、不阻塞启动；`PAPERFLOW_SKIP_BOOTSTRAP=1` 或非 TTY 直接跳过）；`assembly.py` 是对象图装配根（`main()` 的 docstring 写装配顺序）。`__init__.py` 只再导出 `main`，并含一处**必须在任何 grpc 导入前执行**的环境守卫（`GRPC_VERBOSITY=ERROR`，`paperflow.core.rag` → pymilvus 之前）。装配模块叫 `assembly.py` 而非 `main.py`——后者会与再导出的 `main` 函数同名，挡住 pytest `monkeypatch` 的逐段解析。
- **config/**：`sections.py` 是唯一声明点，`loader.py` 是三级优先级加载（默认值 < YAML < env）与 env 派生；只认标量覆写（dict/list 仅 YAML、非标量对象两者都不吃）；`workspace` 在 `from_env()` 末尾绝对化。

## Core Rules

- **顶层 `config/` 是全局可调参数的唯一声明点**：任何可调参数先在 `config/sections.py` 的 dataclass 树加字段（含 YAML 覆盖与 env 派生），消费模块直接读 config，不在模块内另设常量副本。**各子包的 `config/` 是另一档**——放该包**组件自己的行为参数**（调用点各给一套、不进 config.yaml），与顶层构成两级作用域：`core/llm/config/`、`core/agent/config/`、`core/skills/config/`。
- **懒加载单例**：重量组件（RAGService、CitationManager、记忆工具）用双重检查加锁的模块级单例，包导入期不拉重型依赖。
- **依赖方向**：terminal/tools 是被消费方；core 依赖 tools 抽象但不反向；citations 经门面（`get_citation_manager`）对外，内部结构不外泄。**core 不 import 上层模块**（tools/terminal/citations）——这是「LLM 客户端不许搬进 `core/agent`」那类判断的硬依据。注意这条规则**不含 `rag`/`vision`**：两者已住进 `core/`（rag 的媒体块依赖 vision 取图与视觉描述，分开摆会让 `core/rag → vision → core/llm` 成环），core 内部彼此依赖是允许的；RAG 的对外仍然是门面 `get_rag_service()`。
- **枚举一枚举一文件**：各模块 `constants/enums/<name>.py`，`__init__.py` 只做再导出；只有唯一消费方的常量就地声明，不进 `constants/`。
- **错误降级为文本**：工具与中间件层的错误转 ToolResult 文本回传 LLM，只有 `MaxTurnsExceeded` 向上抛。

## Investigation Rule

1. 先读本文件定位子包，再进对应子包 `AGENTS.md`。
2. 装配问题从 `cli/assembly.py` 读起（顺序即依赖方向）；启动预检问题查 `cli/bootstrap.py`；参数问题查 `config/` 与根 manifest 的配置字段表。
3. 改 `cli/` 与 `config/` 的内部模块名时，**`tests/` 里的 monkeypatch 字符串目标要跟着改**（pytest 按「查找处」解析：指错模块不报错，只会让假件静默失效；`from X import f` 之后要 patch 的是**导入方**的全局名）。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 下级：[`core/AGENTS.md`](core/AGENTS.md)〔其内 [`core/rag/AGENTS.md`](core/rag/AGENTS.md) / [`core/vision/AGENTS.md`](core/vision/AGENTS.md)〕 / [`tools/AGENTS.md`](tools/AGENTS.md) / [`citations/AGENTS.md`](citations/AGENTS.md) / [`terminal/AGENTS.md`](terminal/AGENTS.md)
