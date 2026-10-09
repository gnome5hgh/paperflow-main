# paperflow/terminal/

## Scope

- 本文件覆盖 `terminal/` 终端交互隔离层：REPL 主循环、输入适配、输出渲染、确认中心与斜杠命令。设计目标是 I/O 全部可注入、可测试。

## Directory Structure

```text
terminal/
├─ repl/                # 交互半区
│   ├─ loop.py          # 主循环：读输入 → 斜杠命令分发 → supervisor.run → 渲染；Ctrl+C 三态
│   ├─ console.py       # 开场与逐行输出：打印函数工厂 + 路径缩写 + 启动横幅
│   └─ resume.py        # --resume 屏上历史回放：窗口投影成 (role, text) 渲染进滚动区（只读）
├─ confirm/             # 确认域
│   ├─ center.py        # 确认中心：全进程确认的唯一消费者（根治并行 agent 下 rich Live 与 prompt_toolkit 互相干扰/死锁）+ Agent 侧确认回调
│   └─ presentation.py  # 确认框呈现：提示行 + 写/编辑的 diff 预览
├─ commands/            # 斜杠命令
│   ├─ registry.py      # 命令表基础设施：数据结构 + 首 token 等价匹配分发
│   └─ builtin.py       # 内置命令 handler + 默认注册表（新增内置命令在此登记）
├─ io/                  # 输入适配
│   ├─ base.py          # InputIO 契约 + 并发确认锁（两种实现共用）
│   ├─ interactive.py   # PromptToolkitIO（TTY，multiline+历史）+ 主输入/确认两套键绑定
│   └─ fallback.py      # FallbackIO（非 TTY，内置 input）
├─ render/              # 输出渲染
│   ├─ base.py          # BlockRenderer 契约
│   ├─ blocks.py        # PlainBlock（纯文本增量）+ RichBlock（rich Live）
│   ├─ renderer.py      # StreamRenderer：活动流状态机、节流重绘、should_print 三态
│   └─ activity.py      # 工具事件 → 活动行映射（纯函数）：工具名→图标+动词+计数词的唯一领域知识
└─ common/              # 被多个子包横向消费的纯工具
    ├─ diff.py          # compute_diff(unified ±3) + truncate_diff(≤200 行)
    └─ errors.py        # translate_error：API 异常 → 用户语言
```

分层约定：**包根只留 `__init__.py`**（每个关注点都落在子包里），子包 `__init__` 只做再导出与装配；`paperflow.terminal.io` / `paperflow.terminal.render` 由包级 `__init__` 再导出轻量公开接口，`repl` 不做包级导出（依赖 `core.agent`）。

## Core Rules

- **渲染与输入互斥**：rich Live 与 prompt_toolkit 并发互相干扰——确认框/输入框出现前必须 `suspend()`；并行子 agent 的 confirm 由确认中心统一消费，不再各自起临时 prompt。
- **事件驱动渲染**：渲染器只消费 `StreamEvent`（content/tool），不反向感知 agent 内部状态；活动行语义（聚合/慢操作标注/子 agent 前缀）集中在 `render/activity.py` 的映射表。
- **斜杠命令等价匹配**：首 token 等价（非前缀/非包含）；未注册命令打提示后当普通任务送进意图管线；handler 异常就地翻译打印，不杀 REPL。
- **resume 回放只读**：回放只做数据投影与渲染，绝不改 SQL 会话状态——恢复语义仍由 memory 层唯一决定。
- **回调注入**：REPL 把 confirm 回调注入 Agent（子 agent 经 spawn 继承）；回调缺失时工具侧 fail-safe，绝不挂起。
- **确认域自洽**：确认中心与它的 Agent 侧回调、确认框呈现同处 `confirm/`——不要让它反向依赖 `repl/`（那会把并行确认死锁的修复理由绕回去）。

## Key Entry Points

- `repl/loop.py` — 每轮主循环（含 sleeptime `run_once_if_due()` 检查）
- `confirm/center.py` — 确认唯一消费者
- `commands/builtin.py` — 斜杠命令注册表（新增内置命令在此登记）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 被驱动的运行层：[`../core/AGENTS.md`](../core/AGENTS.md)（agent/runtime.py）
