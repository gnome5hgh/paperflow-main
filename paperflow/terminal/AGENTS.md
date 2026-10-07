# paperflow/terminal/

## Scope

- 本文件覆盖 `terminal/` 终端交互隔离层：REPL 主循环、输入/渲染、确认中心与斜杠命令。设计目标是 I/O 全部可注入、可测试。

## Directory Structure

```text
terminal/
├─ repl.py            # 交互半区：读输入 → 驱动 supervisor → 渲染输出；cli.py 只留装配组合根
├─ io.py              # InputIO 契约：PromptToolkitIO(TTY, multiline+历史) vs FallbackIO(非 TTY)；按 stdin.isatty() 二选一
├─ render.py          # StreamRenderer（线程安全）：TTY=rich Live 活动流 / 非 TTY=增量追加；suspend() 在确认框前停 live
├─ activity.py        # 工具事件 → 活动行映射（纯函数）：工具名→图标+动词+计数词的唯一领域知识
├─ confirm_center.py  # 确认中心：全进程确认/提问的唯一消费者（根治并行 agent 下 rich Live 与 prompt_toolkit 互相干扰/死锁）
├─ commands.py        # REPL 斜杠命令：注册表 + 内置命令；CommandContext 依赖注入袋，handler 不闭包抓主循环变量
├─ resume.py          # --resume 屏上历史回放：把 in-context 窗口投影成 (role, text) 渲染进滚动区（只读，不改会话状态）
├─ diff.py            # compute_diff(unified ±3) + truncate_diff(≤200 行)：写/编辑确认前预览
└─ errors.py          # 终端层错误类型
```

## Core Rules

- **渲染与输入互斥**：rich Live 与 prompt_toolkit 并发互相干扰——确认框/输入框出现前必须 `suspend()`；并行子 agent 的 confirm/ask 由确认中心统一消费，不再各自起临时 prompt。
- **事件驱动渲染**：渲染器只消费 `StreamEvent`（content/tool），不反向感知 agent 内部状态；活动行语义（聚合/慢操作标注/子 agent 前缀）集中在 `activity.py` 映射表。
- **斜杠命令等价匹配**：首 token 等价（非前缀/非包含）；未注册命令打提示后当普通任务送意图管线；handler 异常就地翻译打印，不杀 REPL。
- **resume 回放只读**：回放只做数据投影与渲染，绝不改 SQL 会话状态——恢复语义仍由 memory 层唯一决定。
- **回调注入**：REPL 把 confirm/ask 回调注入 Agent（子 agent 经 spawn 继承）；回调缺失时工具侧 fail-safe，绝不挂起。

## Key Entry Points

- `repl.py` — 每轮主循环（含 sleeptime `run_once_if_due()` 检查）
- `confirm_center.py` — 确认/提问唯一消费者
- `commands.py` — 斜杠命令注册表（新增内置命令在此登记）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 被驱动的运行层：[`../core/AGENTS.md`](../core/AGENTS.md)（agent/runtime.py）
