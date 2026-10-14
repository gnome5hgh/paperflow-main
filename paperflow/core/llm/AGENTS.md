# paperflow/core/llm

## Scope

- 覆盖 `core/llm/`：LLM 接入层——wire 消息家族、异步客户端与结构化输出。
- 本层只放子包，不放裸模块。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：把 OpenAI 兼容端点封装成异步客户端，并定义与模型交换的消息类型、响应信封与 JSON 抽取能力。

## Directory Structure

```text
llm/
├─ domain/dto/   # messages/（wire 消息家族：基类 + system/user/assistant/tool 四子类）
│                #   + 响应信封与 token 用量（层间载体，不是消息类型）
├─ config/       # StructuredOutputConfig（组件自有参数，不进 config.yaml）
└─ services/     # client.py（异步客户端，非流式与流式两个模式）
                 #   + structured.py（结构化输出 / JSON 抽取）
```

## Responsibilities

- 提供 `chat()` 与 `chat_stream()`，以及把工具转成 OpenAI function calling JSON Schema 的出口。
- 定义消息的进出漏斗与角色的类型化表达。
- 承载响应信封：结束原因、token 用量、模型快照、思维链、拒答、补全 id。

## Core Logic

- **消息的进出各只有一个漏斗**：出站经唯一的 wire 出口，入站经唯一的反向还原点（持久化层存的是 role 字符串，回放时在那里还原成具体类型）。新增消息字段要同时过这两个地方，否则字段会静默丢一半。
- **角色由类型固定**：四个角色子类各自钉死自己的 `role`，角色特有字段只出现在对应子类上。**判角色用类型判断，不要比较字符串**——比较字符串会把「类型写错」重新变回一个运行时才炸的 bug。
- **进与出共用同一族类型**：返回的就是一条 assistant 消息，它随即被追加进 in-context 窗口、下一轮原样发回去。不要为「回复」另立类型——wire 上本来就是同一个形状。
- **响应信封不上 wire，但随 assistant 消息落盘**：出站只取正文与 `tool_calls`。**截断是从结束原因派生的属性，不要另存一个字段**（两处必然漂）。要按会话汇总用量、或事后查某次回答由哪个模型快照产出，读持久化消息上的那份信封。
- **参数降级重试**：端点不支持 `response_format` / `extra_body` / `stream_options` 时，去掉该参数重试一次。各家兼容端点的措辞不同，靠试、别靠猜，也别为此加配置项。

## Key Entry Points

- `services/client.py` — 两个模式的客户端
- `services/structured.py` — 结构化输出
- `domain/dto/messages/` — 消息家族与 wire 转换
- `domain/dto/` — 响应信封与用量

## Usage And Change Notes

- **`on_delta` 跑在线程池线程里**：它只能追加或打印，**不要碰事件循环**（不能 await、不要直接改异步状态）。
- **api_key 留空即 fail-fast**，且要给出可行动的配置指引（配哪个字段、或哪个环境变量），不要退化成一句「未配置」。
- 出网有统一出口（`core/security/services/` 的端点守卫），**不要在客户端里自建连接绕过它**。
- 新增端点兼容性适配时，先看是否已由参数降级重试覆盖，不要为某个端点写特例分支。

## Dependencies And Collaboration

- 消费方：`core/agent`（ReAct 循环的每次模型调用）、`core/memory`（消息类型的落盘与回放）、`core/rag` 与 `core/vision`（经 `structured` 做 JSON 抽取）。
- 出网经安全侧的统一下载/请求出口；本包不实现网络策略。

## Investigation Rule

- 客户端与两个模式看 `services/client.py`；结构化输出与 JSON 抽取看 `services/structured.py`；消息类型与 wire 转换看 `domain/dto/messages/`。
- 消息字段在落盘后丢失时，先查上面那两个漏斗是否都覆盖了该字段。
- 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core -k "llm or message or structured" -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 设计依据：ADR 0006（结构化输出）
- 会话落盘与回放：[`../memory/AGENTS.md`](../memory/AGENTS.md)

## References

- [`services/client.py`](services/client.py)
- [`services/structured.py`](services/structured.py)
- [`domain/dto/`](domain/dto/)
- [`../memory/AGENTS.md`](../memory/AGENTS.md)
