# paperflow/tools

## Scope

- 覆盖 `tools/` 原子工具层：**一工具一文件、按域分包**。
- 工具的实现可以住在别的包（比如读 PDF 在 `core/rag/parsers/`），本层负责把它封成 Tool。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：把能力暴露成模型可调用的工具——声明参数 schema 与安全元数据，并把结果降级为文本回传。

## Directory Structure

- `file/` — 读 / 写 / 编辑 / glob / grep / 读 PDF / 取标题（+ 原子写盘）
- `search/` — 下载 PDF（含 SSRF 校验与写盘后处理）
- `review/` — 审查裁决、模板体检、期刊会议等级查询
- `citations/` — 引用工具（只装配给 `citation-agent`）
- `rag/` — 检索、看图、入库、全量收敛、索引体检（只装配给 `rag-agent`）
- `vision/` — 图表提取与看图分析
- `memory/` — 记忆工具集（只装配给 `memory-agent`）
- `orchestration/` — 子 agent 派发（唯一的调度工具）
- `skills/` — 加载 skill 正文与资源
- `common/` — 装配工厂与共享 HTTP 基础设施

## Responsibilities

- 为每个工具声明名字、描述、参数 schema、安全元数据与风险档位。
- 把工具结果（含各种失败）统一成结构化结果回传模型。
- 提供装配工厂，由装配层决定「哪个 agent 拿到哪些工具」。

## Core Logic

- **`__init__.py` 统一再导出**：消费方从包门面导入，符号名稳定，内部路径随便拆。
- **装配一律经装配工厂**：它按语义根提示生成目录提示、给写工具盖上默认写根、注入配置。**agent 拿到什么工具由装配层决定**，工具内部不要自作主张。
- **默认写根只是兜底**：任务文本点名了保存位置就用它（下载类工具有专门的落点参数，其余显式传目录或路径），只有没指定时才落到默认根。工具描述要在动作发生处写清这一点。
- **安全元数据必填**（字段定义、取值来源与校验在 [`../core/tool/AGENTS.md`](../core/tool/AGENTS.md)）：写 / 删类一律高档位；可重入的批量追加不强求。档位高于会话免问档即逐次确认——**降档不是旁路**。
- **来源声明「结果是不是外部数据」**：**只有读外部数据的工具才声明**；自家算出的结构化结果不声明——全标会让标记变成噪声。
- **参数级的格式声明是中间件的接管开关**：路径 → 路径权威、内容 → 内容防护、命令 → 命令防护。新工具不需要写安全代码，声明格式即可被接管；**声明错格式等于没被接管**。
- **子 agent 只能经派发工具调用**：派发闸门全在 `orchestration/spawn.py`（未知类型 → 白名单 → 同批去重 → 审稿预算 → 每轮上限）。**顺序与并行归 supervisor 自主，闸门只兜上限**；轮数预算下沉代码，不靠 LLM 自律。同路径写互斥**不在闸里**——写工具按真实写目标登记写占用、跨实例当场拒绝，判定在运行时执行层。
- **失败一律降级为文本**：工具异常转成错误结果回传模型自行决策。依赖的运行时上下文缺失时 fail-safe 返回提示，**绝不挂起**。

## Key Entry Points

- `common/` — 装配工厂
- `orchestration/spawn.py` — 派发工具与五道闸
- `memory/` — 记忆工具集（惰性单例）
- `__init__.py` — 全量再导出

## Usage And Change Notes

- 新增工具：写文件 → 声明安全元数据与参数格式 → 在包 `__init__` 登记 → 在装配层决定装给谁。**只装给该领域责任人**，其余角色需要时派发它。
- 工具的运行时上下文（记忆、review 所需的 skill 注册表等）由装配层注入；缺失时要降级为提示，不要抛异常。
- 工具的依赖注入点是 `_config` 与各运行时上下文，不要在工具内直接构造重量组件。

## Dependencies And Collaboration

- 依赖 `core/tool/`（抽象与安全元数据校验）、`core/constants/`（风险与副作用词汇）、`core/security/`（中间件在调用前后接管）。
- 被 `cli/` 在装配期取用，被 `core/agent` 在 ReAct 循环里执行。
- 各领域工具的真实实现对在对应 core 子包里（记忆在 `core/memory/`，检索在 `core/rag/`，引用在 `citations/`）。

## Investigation Rule

1. 先看目标工具的文件头与安全元数据声明，再看它的 `execute`。
2. 装配问题看 `common/`；派发问题看 `orchestration/spawn.py`；记忆工具集看 `memory/`。
3. 工具读 PDF / 抽标题的实现不在本层，在 `core/rag/parsers/`——本层只是把它封成 Tool。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/tools -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/tools -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 插件侧的工具装配：[`../../agents/AGENTS.md`](../../agents/AGENTS.md)
- 运行层（安全中间件、记忆、意图）：[`../core/AGENTS.md`](../core/AGENTS.md)

## References

- [`common/`](common/)
- [`orchestration/spawn.py`](orchestration/spawn.py)
- [`memory/`](memory/)
- [`../core/AGENTS.md`](../core/AGENTS.md)
