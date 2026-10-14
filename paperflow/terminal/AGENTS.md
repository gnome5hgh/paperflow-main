# paperflow/terminal

## Scope

- 覆盖 `terminal/` 终端交互隔离层：REPL 主循环、输入适配、输出渲染、确认中心与斜杠命令。
- 设计目标是 **I/O 全部可注入、可测试**——新增交互能力照这个前提做，不要在实现里直接摸 stdin/stdout。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：承载用户与 agent 之间的全部交互——读输入、渲染活动流、弹确认框、解析斜杠命令、回放历史会话。

## Directory Structure

- `repl/` — 每轮主循环、开场与逐行输出、`--resume` 的屏上历史回放
- `confirm/` — 确认中心（全进程确认的唯一消费者）与确认框呈现
- `commands/` — 斜杠命令表与内置命令
- `io/` — 输入适配：TTY 实现与非 TTY 后备，以及并发确认锁
- `render/` — 渲染：块渲染契约、两种实现、活动流状态机、工具事件到活动行的映射
- `common/` — 被多个子包横向消费的纯工具（diff 计算与截断、异常翻译）

## Responsibilities

- 驱动一轮对话：读输入 → 分发斜杠命令 → 跑根 agent → 渲染。
- 把 agent 事件流渲染成活动流，并把工具调用显示成活动行。
- 串行化并呈现所有确认请求（含并行子 agent 的）。
- 回放历史会话（只读投影）。

## Core Logic

- **渲染与输入互斥**：富文本 live 渲染与输入框库并发会互相干扰——确认框 / 输入框出现前必须挂起渲染。并行子 agent 的确认由确认中心统一消费，**不要各自起临时 prompt**。
- **每块一个渲染实例**：live 渲染停止后仍保留上次渲染的高度，复用同一实例再启动时，首帧会按这个高度把光标上移并逐行擦除——擦掉的正是两块之间打印的内容（用户回显、工具行、上一轮答案尾部）。所以每次开块换新实例。
- **事件驱动渲染**：渲染器只消费 agent 事件（正文 / 工具），不反向感知 agent 内部状态。**事件只装事实、不装排版**；活动行语义（聚合、慢操作标注、子 agent 前缀）集中在 `render/` 的映射表里。

## Key Entry Points

- `repl/loop.py` — 每轮主循环（本轮收尾会调记忆整合）
- `confirm/center.py` — 确认的唯一消费者
- `commands/builtin.py` — 斜杠命令注册表（新增内置命令在此登记）
- `render/activity.py` — 工具事件 → 活动行的映射

## Usage And Change Notes

- **包根只留 `__init__.py`**，每个关注点落在子包里，子包 `__init__` 只做再导出与装配。
- `io` 与 `render` 由包级 `__init__` 再导出轻量公开接口；`repl` **不做**包级导出（它依赖 `core.agent`）——这个区别是刻意的，别「统一」掉。
- **斜杠命令按首 token 等价匹配**（不是前缀、不是包含）；未注册的命令打提示后当普通任务送进管线；handler 异常就地翻译打印，不杀 REPL。
- **`--resume` 回放只读**：只做数据投影与渲染，绝不改 SQL 会话状态——恢复语义由 memory 层唯一决定。
- **回调注入**：REPL 把确认回调注入 Agent（子 agent 经派发继承）；回调缺失时工具侧 fail-safe，绝不挂起。
- **确认域自洽**：确认中心、它的 Agent 侧回调、确认框呈现同处 `confirm/`，**不要让它反向依赖 `repl/`**——那会把「并行确认死锁」的修复理由绕回去。

## Dependencies And Collaboration

- 被 `cli/` 启动；驱动 `core/agent`（每次用户输入跑一轮）。
- 消费 `core/agent` 的流式事件与 `core/memory` 的会话数据（回放）。
- 是全进程确认请求的唯一出口：安全侧只产出确认请求，弹框由这里消费。

## Investigation Rule

1. 先分清是「输入不工作」还是「渲染不对」：前者看 `io/` 与 `repl/`，后者看 `render/` 与 `confirm/`。
2. 主循环看 `repl/loop.py`；确认看 `confirm/center.py`；命令不生效看 `commands/registry.py`。
3. 排查渲染错乱先确认是否在确认框前后正确挂起 / 恢复了 live 渲染。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/terminal -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/terminal -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 被驱动的运行层：[`../core/AGENTS.md`](../core/AGENTS.md)
- 确认语义（安全侧）：[`../core/security/AGENTS.md`](../core/security/AGENTS.md)

## References

- [`repl/loop.py`](repl/loop.py)
- [`confirm/center.py`](confirm/center.py)
- [`commands/builtin.py`](commands/builtin.py)
- [`render/activity.py`](render/activity.py)
