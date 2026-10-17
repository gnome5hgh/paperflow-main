# paperflow/core/memory

## Scope

- 覆盖 `core/memory/`：Letta 记忆栈的移植——SQLite 持久化、记忆块与会话消息、上下文压缩、记忆整合。
- 根 manifest 的两条硬红线（SQL 是真相源、读—改—写必须走一次持锁事务）在本层同样成立，这里写本包特有的。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：为 agent 提供跨轮记忆——可编辑的记忆块、可回放的会话消息、上下文压缩与记忆整合。

## Directory Structure

```text
memory/
├─ constants/    # 工具名集合与块的播种文案
├─ schemas/      # 数据模型：块 / 记忆 / 持久化消息 / Agent 状态
├─ storage/      # SQLite 持久化（库、表结构、增量迁移、messages_fts 全文索引）
├─ common/       # 领域异常（并发写冲突、块字符上限）
└─ services/     # 各管理器 + compaction.py（上下文压缩）+ consolidation.py（记忆整合）
                 #   + consolidation_queue.py（轮末 debounce 队列）
```

## Responsibilities

- 落盘全部会话消息，并按 in-context 窗口回放（Recall 完整）；消息按 `session_id` 分组，同一会话的 supervisor 与子 agent 共享分组、各自独立 `agent_id`。
- 提供记忆块的增删改查、版本快照与并发保护。
- 在窗口过长时压缩 in-context（只改窗口，不删 SQL 行）。
- 提供会话内关键词召回（FTS5 trigram，不可用时 LIKE 降级），永远按 `session_id` 过滤。
- 每轮收尾把新对话排进后台 debounce 队列，空闲窗口后整合进记忆块。

## Core Logic

- **SQL 是真相源，markdown 是投影**：MemFS 生成的 markdown 可手工编辑并回写块，但方向永远是「块 → 文件」是派生，别把真相源搞反。
- **块写入是持锁的原子读—改—写 + 写入 CAS**：追加/替换类操作把「读旧值→算新值→写回」整段放进一次事务（未经该入口直接写库，会让两个并发写者互相抹掉）。写前留历史快照（供撤销），只读块拒绝读写，块长有上限。
- **压缩只动 in-context 窗口，永不删 SQL 行**：冻结旧摘要段、只压新增段，保留近期尾部；摘要对齐 Claude Code 的 9 节，其中「All user messages」由代码从 SQL 逐字插入；并保持 tool 消息与它对应的结果成对，别留下孤儿。摘要生成失败时保持原窗口，不拿空摘要替换历史。
- **召回只搜当前会话**：`session_id` 是检索的作用域边界，不提供跨会话参数；跨会话知识由 blocks 承担。FTS5 用 trigram（中文子串），索引不可用时回退 SQL LIKE。
- **整合是后台 debounce 队列**：轮末只 enqueue（按会话合并，默认 30s 静默窗口、300s 最大延迟），退出/SIGINT/异常路径必须 flush；整合算法本身仍是游标 + 行级编辑，队列不承载消息。
- **任务锚点不写入消息表**：压缩时与摘要同一次调用抽出目标/硬约束/截止时间/未完成步骤，存 `agent_state.task_anchor`，每轮请求级注入。
- **记忆整合每轮收尾跑一次，空批次即 NOOP**：本轮没有新消息就不调模型。输入是「游标之后的新消息 + **全部可写块的当前值**」——喂旧值是「不产生矛盾条目」的前提，不要为了省 token 只喂增量。
- **整合产出的是行级指令，不是整块重写**：新增 / 按行首前缀替换 / 删除，整块重写仅限特定两个块。目标分册名由代码按「类型 + 当天日期」拼装、写满自动续号；**被取代或删除的条目要跨整个分册家族（含往日分册）去找**，空掉的分册整块删除——这样「全量喂旧记忆」的总量才有界。
- **失败处理是不对称的**：抽取阶段失败要保留游标、下次重试（连败几次后强制推进防死锁）；应用阶段失败照常推进游标（重放会产生重复写入）。改整合流程时别把这条弄反。
- **同轮即时生效**：每轮开头重建记忆块并替换 head，所以同一轮里改过的块，下一轮模型调用就能看见。改 head 构造顺序时别破坏这个时机。

## Key Entry Points

- `storage/` — 库、表结构与增量迁移
- `services/compaction.py` — 上下文压缩（9 节摘要、锚点抽取、分段冻结）
- `services/consolidation_queue.py` — 轮末 debounce 整合队列
- `services/message_manager.py` — 落盘、窗口回放、会话内检索
- `services/consolidation.py` — 记忆整合
- `services/` — 块与会话管理器（记忆工具的底座）

## Usage And Change Notes

- **给已有表加列，必须同时登记到增量迁移表**：建表脚本只管新库。**漏登记的症状是老库读写时报「没有那一列」，新库上完全测不出来**——所以这类改动要拿旧库试一次。
- **落盘不做 JSON 类型猜测**：持久化消息的正文恒为字符串或空，别按内容形状去猜类型。响应信封以 JSON 一列存放，旧行为空是正常的，不要当成脏数据。
- 记忆工具的运行时上下文在 `paperflow/tools/memory/`；本包只提供管理器，不提供工具封装。

## Dependencies And Collaboration

- 消费方：`core/agent`（in-context 窗口与压缩、任务锚点）、`tools/memory/` 与 `tools/recall/`（记忆工具 / 只读召回）、`terminal/repl`（轮末 enqueue 与退出 flush）。
- 与 `core/llm` 的关系是单向的：消费它的消息类型做落盘与回放。
- 与 RAG 无耦合：记忆检索是 SQL，不用向量。

## Investigation Rule

1. 先读本文件，再按问题落到具体服务。
2. 库、表结构与增量迁移看 `storage/`；块读写与并发控制看 `services/`；压缩看 `services/compaction.py`；整合看 `services/consolidation.py`。
3. 排查「改了没生效」先确认改动是否走了持锁入口；排查老库报错先看增量迁移登记。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/memory -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/memory -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 设计依据：ADR 0004（记忆系统）
- 工具封装与装配：[`../../tools/AGENTS.md`](../../tools/AGENTS.md)、[`../../../agents/AGENTS.md`](../../../agents/AGENTS.md)

## References

- [`storage/`](storage/)
- [`services/compaction.py`](services/compaction.py)
- [`services/consolidation.py`](services/consolidation.py)
- [`schemas/`](schemas/)
