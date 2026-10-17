# paperflow/core/tool

## Scope

- 覆盖 `core/tool/`：**Tool 抽象本身**——基类契约、安全元数据、结构化结果与加载期校验。
- 具体工具怎么写、装给谁、怎么被派发，见 [`../../tools/AGENTS.md`](../../tools/AGENTS.md)。

## Module Positioning

- 角色：`bom-or-contract-leaf-manifest-dir`
- 职责：定义所有工具共同遵守的契约，并让「工具自称的安全属性」在加载期就被校验、在运行期被安全中间件消费。

## Directory Structure

- `domain/` — 本领域的公共词汇，按后端分层惯例分两个子包：
  - `entity/tool.py` — `Tool` 抽象基类与它的安全元数据字段（工具这一业务里「一个东西」的模型）
  - `dto/tool_result.py` — 工具结果（正文 + 结构化摘要两条出口）
- `services/validation.py` — 元数据的加载期校验
- `constants/constants.py` — 摘要里跨模块读写的键名（对照取值见 `../constants/`）

`domain/` 下**暂不设 `bo/` 与 `vo/`**：`bo`（领域内中间态，不落库不出站）在本包尚无 occupant
——`ToolResult.summary` 的结构化载荷是键名契约加 dict、不是类型；`vo`（面向调用方的展示模型）
同样没有——终端侧由 `AgentEvent` 经纯函数映射成活动行。将来真出现第一个 occupant 再建对应
子包，不预置空桶。

## Responsibilities

- 固定工具的声明面：名字、描述、参数 JSON Schema、执行入口、安全元数据。
- 把「非法声明」变成加载期的 fail-fast，而不是运行期的静默失效。
- 提供结构化结果通道，让决策、摘要与领域数据不必挤进给模型看的正文。

## Core Logic

- **安全元数据必填**：风险档、副作用、来源都要声明，取值来自 `core/constants/` 的枚举（单一真相源）。注册表加载时校验合法值——写错在这里就报错，不留给运行期。
- **风险档是「问不问」的来源之一**：它是策略引擎的输入，不是装饰；新增工具时必须给出真实档位，不要一律填低。
- **副作用字段是「谁在写」的判据**：写互斥与写围栏共用同一份判定，改这个字段等于改并发语义。
- **来源字段声明「结果是不是外部数据」**：声明了来源的结果会被内容防护加横幅并过确定性扫描，所以只在该声明的工具上声明——全标会让标记变成噪声。
- **结果有两条出口**：正文回给模型；结构化摘要给框架用。摘要里跨模块读写的键名是契约，**写错一个字符串的后果是功能静默不生效**（例如裁决已提交、循环却不终止）。
- **提示性字段不参与强制**：语义根的提示只生成给模型看的目录提示，真正的路径权威在安全中间件里——别把提示当围栏。
- **大工具结果外置**：`Tool.max_result_size_chars` 可覆写全局阈值；原文由 `services/tool_result_store.py` 写 MinIO（本地回退），消息里换成带 `tool_call_id` 的指针。外置在 `ToolExecutor` 的 after 钩子之后发生，审计仍看得到原文；摘要不携带工具引用。

## Key Entry Points

- `domain/entity/tool.py` — `Tool` 基类与元数据字段
- `domain/dto/tool_result.py` — 结果类型（正文 + 摘要）
- `services/validation.py` — 加载期校验
- `constants/constants.py` — 摘要的跨模块键

## Usage And Change Notes

- **新增一个元数据字段要同时改三处**：基类字段、加载期校验的合法值、取值枚举（在 `core/constants/`）。只改一处会让校验形同虚设或让新字段永远为空。
- 改摘要键名要连所有消费方一起改（它们在跨模块两侧读写），改动不要留旧键名兼容分支。
- 本包只放契约与数据载体；**行为（怎么执行、怎么拦）不在这里**——执行管道在 `core/agent/services/`，拦截在 `core/security/`。

## Dependencies And Collaboration

- 被 `tools/`（所有具体工具）、`core/agent`（执行管道）、`core/security`（读元数据做判定）、`core/skills` 与 `core/mcp`（第三方工具桥接后同样遵守本契约）消费。
- 依赖 `core/constants/` 的取值枚举；本包不依赖任何上层模块。

## Investigation Rule

1. 工具「声明了什么」看具体工具文件；「声明合不合法」看 `services/validation.py` 与本包字段定义。
2. 安全判定用的档位/副作用/来源从 `core/constants/` 取，排查取值问题时从那里核对。
3. 结果结构化数据对不上，先看 `constants.py` 的键名与消费方是否一致。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core -k "tool" -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core -k "tool" -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 具体工具与装配：[`../../tools/AGENTS.md`](../../tools/AGENTS.md)
- 安全侧消费：[`../security/AGENTS.md`](../security/AGENTS.md)

## References

- [`domain/entity/tool.py`](domain/entity/tool.py)
- [`domain/dto/tool_result.py`](domain/dto/tool_result.py)
- [`services/validation.py`](services/validation.py)
- [`../../tools/AGENTS.md`](../../tools/AGENTS.md)
