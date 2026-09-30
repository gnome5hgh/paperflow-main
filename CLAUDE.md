# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dev dependencies (in conda env)
conda run -n paperflow pip install -e ".[dev]"

# Run all tests
conda run -n paperflow python -m pytest tests/ -v

# Run a single test
conda run -n paperflow python -m pytest tests/agent/test_agent.py::TestExecTool -v

# Start 依赖服务栈（Milvus Standalone RAG 向量库 + GROBID PDF 解析）；单测用 Milvus Lite 内嵌，无需此服务
# `paperflow` 启动时会自动拉起，此命令用于手动管理
# GROBID 首次需先一次性初始化（复制 grobid-home 到 data/grobid/），见 docs/测试指南/ §2.3
docker compose up -d

# Run the app — 交互式 REPL（⚠️ 不能经 conda run）
# conda run 不转发 stdin 给子进程 → 交互式 REPL 的 input() 立即 EOF 退出。必须先激活 env：
conda activate paperflow && paperflow
# 等价：python -m paperflow（同一 env 内）
# 启动时自动探测依赖服务（Milvus/GROBID），未起则自动 docker compose up -d 并等待健康；
# 失败只警告不阻塞（软依赖降级）。手动管理服务仍用 docker compose up -d；
# 跳过预检：PAPERFLOW_SKIP_BOOTSTRAP=1
```

Always use `conda run -n paperflow` for 非交互命令（测试/脚本/安装）——never bare `python` or `pip`. **例外：交互式 REPL（`paperflow`）不能经 `conda run`**——它不转发 stdin，REPL 一启动就 EOF 退出；需 `conda activate paperflow` 后直接 `paperflow`。

**API key 配置**：key 从 `.env`（gitignored，复制 `.env.example` 填 `PAPERFLOW_API_KEY`）或环境变量 `PAPERFLOW_API_KEY` 读取，**不硬编码在代码里**。未配置时启动即报「LLM API key 未配置」。

## 文档同步规则

修改代码时，必须同步更新关联的设计文档：

- **新增/修改任何代码后，都要先思考是否需要同步更新 ADR / spec / plan**：接口签名、行为语义、结构或已记录决策发生变化都算。需要同步时，在**测试代码通过后**再更新对应文档——先让代码行为被测试锁住，再让文档描述现状
- 如果在实现过程中发现 Layer N 的 spec/plan 文档与最终代码不一致，修改代码后需同步更新对应的 spec（`docs/superpowers/specs/`）和 plan（`docs/superpowers/plans/`）
- 如果当前 Layer 的修改影响了上层 Layer 的 spec/plan（如 Layer 1 实现时发现 Layer 0 的接口需要调整），同样需要回修受影响的上层文档
- ADR（`docs/adr/`）维护规则：
  - **按模块划分，一份 ADR = 一个模块**：0001 RAG、0002 安全、0003 ReAct 多 Agent 架构（引擎）、0004 记忆、0005 SubAgent×Tool 映射、0006 结构化输出、0007 意图识别、0008 引用管理、0009 图片提取与多模态、0010 终端与 CLI 入口。新模块决策扩展对应模块的 ADR；确属新模块时新建，编号顺延
  - **正文恒为最新设计**：设计变更直接改写 ADR 正文对应小节，使读者任何时候读到的都是模块现状——不在正文写「修正说明 / 修正（日期）」等历史痕迹，也不在正文叙事里写「原方案被废弃」
  - **修正史统一进 `docs/adr/修正记录.md`**：每次改写正文，在该文件按时间追加一条（日期 / 模块 ADR / 变更摘要 / 来源 spec 或决策出处），保留「为什么变成现在这样」的追溯通道
  - **跨模块变更按归属拆分**写入各模块 ADR，不设跨模块 ADR；每个 ADR 与相邻模块的分工边界在文中显式声明（如确认中心：终端侧架构在 0010、安全语义在 0002）
  - **编号不回收、空缺须补齐**：删除或合并 ADR 后重编号（顺延补齐空位），并同步更新所有活文档引用（CLAUDE.md 设计文档索引、CONTEXT.md、docs/learning、修复清单）；历史 spec/plan 中的旧编号是历史记录，不回改
  - 文中互相引用只写「见 ADR XXXX」，不写「见 ADR XXXX 修正说明」——修正史在修正记录文件里，不在 ADR 正文

## Code style

注释面向所有人——包括新人和非维护者。用中文写,不出现内部项目代号(任务/决策/评审编号、日期、spec/ADR 章节引用),也不写过程流水账。需要背景时用一句白话说明。

每个函数/方法的 docstring 至少说明其**作用**;参数含义不显而易见时说明;返回值复杂时说明「是什么」;关键算法思路、非显而易见的边界条件(安全/并发/失败处理)是重点。非显而易见的逻辑保留 WHY,但用通俗语言,不写成变更日志。

按逻辑块组织注释:同一逻辑块的多行代码合并成一条块注释,放在块首;避免每行一注。每条注释紧跟其描述的代码。

Good comments explain the reason, not the mechanics:
```python
# BAD: "Loop over items and add to result"
# GOOD: "遍历所有 items 并去重,因为多源搜索结果可能包含同一篇论文的不同版本"
```

修改代码时同步更新注释——过期注释比没有注释更糟。

## Architecture

paperFlow 是 LLM 驱动的学术研究流程助手（ADR 0003）。单根 agent（supervisor）接收每一轮用户输入 → 意图识别（INTENT 块注入）→ ReAct 循环 → 拆解子任务 spawn 子 agent（searcher/noter/reviewer/qa-agent/researcher）→ 聚合各子 agent 的结构化摘要（digest）→ 汇总回答。

代码分层（自底向上）:

```
paperflow/
  core/          核心运行层:agent(ReAct 循环) + agent_registry + llm + tool(抽象)
                 + security(安全中间件) + memory(记忆系统) + intent(意图识别)
                 + structured(结构化输出)
  rag/           RAG 检索栈(解析/分块/向量/混合检索),懒加载单例
  citations/     引用管理(溯源落地):bib.py 读写 + corpus.py 语料标题索引
                 + manager.py 编排
  vision/        视觉分析(pdffigures2 提取管线: parsers/ 解析 + detectors/ 图检测 + 编排 + 视觉模型看图)
  tools/         原子工具:file/ search/ review/ rank/ orchestration/ citations/ rag/ vision/ memory/ common
  terminal/      终端交互:InputIO(输入) + StreamRenderer(渲染) + diff
agents/<name>/   Agent 插件:AGENT.md(frontmatter+system_prompt) + tools.py(TOOLS 列表)
skills/          Skill 插件:SKILL.md(agentskills.io 格式) + 可选 tools.py/references/
```

设计文档索引（ADR 按模块划分，正文恒为最新设计，修正史见 docs/adr/修正记录.md）：0001(RAG 管线)、0002(安全设计)、0003(ReAct 多 Agent 架构)、0004(记忆系统)、0005(SubAgent×Tool 映射)、0006(结构化输出)、0007(意图识别)、0008(引用管理)、0009(图片提取与多模态)、0010(终端与 CLI 入口)。

### Agent plugin system

Every agent lives in `agents/<name>/` with two files:
- `AGENT.md` — YAML frontmatter (`name`, `description`, `allowed_agents`, `allowed_spawns`) + Markdown body(契约式结构:派发类 worker 为五段式——身份/边界/能力/交付契约/方法启发式;reviewer/qa-agent 按角色裁剪。编排决策由 LLM 运行时自主,不写跨 agent 编排序列)
- `tools.py` — module-level `TOOLS: list[Tool]` list. Each Tool is a subclass of `Tool` ABC with `name`, `description`, `parameters` (JSON Schema for OpenAI function calling), and `execute(**kwargs) -> ToolResult`

`AgentRegistry(agents_dir)` scans this directory at init time, parses frontmatter, dynamically imports `TOOLS` from each `tools.py`, and exposes `get_config(agent_type) -> AgentConfig` plus `list_agents()`. It is the single entry point for agent plugin discovery — the Skill system keeps a parallel registry (`SkillRegistry`, see below), which registers installed skills rather than agents.

装配时 `Agent.__init__` 在角色定义后拼接全 agent 共有的行为基座 `BASE_PROMPT`(`core/agent/base_prompt.py`:诚实性协议/交付契约语义/协作语义)——通用铁律不重复写在各 AGENT.md。

**Skill 体系**（`paperflow/core/skills/registry.py`）：Skill 是给**现有** agent 注入领域知识/流程指令/轻量工具的可安装能力包（agentskills.io 格式），无独立推理循环——与上面 agent 插件机制是平行而非同一概念。`SkillRegistry(builtin_dir, workspace_dir)` 两级扫描 `skills/`（内置）与 `<workspace>/skills/`（用户安装，`paperflow skill install` 准入通道或手动拷贝），三级渐进披露：L1 `<available_skills>` name+description 清单注入 head（无 skill 零开销）→ L2 `load_skill` 工具按需加载正文 → L3 `load_skill(resource=...)` 读资源（路径围栏限 skill 目录内）。skill 捆绑的 `tools.py` 经 `merge_tools` 并入子 agent 工具表——supervisor 代码级恒不并入（权限最小化红线）；含代码的安装强制人工过目（`-y` 拒绝，须显式 `--allow-code`）。

现有 6 个 agent（`agents/` 下）:

| agent | 职责 | allowed_spawns | 工具要点 |
|---|---|---|---|
| `supervisor` | 调度主管:拆解任务、spawn、汇总 | 硬编码放行所有（绕过白名单） | 仅 2 个调度工具 + 7 个记忆工具（blocks/ 核心块编辑 + `conversation_search`） |
| `searcher` | 多源搜索 → reviewer 门禁 → 可选下载 | `[reviewer]` | web_search + fetch_pdf + ask_user + spawn |
| `noter` | 纯笔记生成：基于指定 PDF 起草结构化笔记,内部 reviewer 审稿 ≤3 轮 | `[reviewer]` | 原子文件工具 + ask_user + spawn + glob/grep + 4 引用工具(lookup/add/format/list) + analyze_figures |
| `researcher` | 选题发现:基于本地语料盘点→survey/gaps→idea 卡→外部新颖性验证(源优先 semantic scholar,失败如实标「未经外部验证」)→研究计划,产物自己落盘 research 根,内部 spawn searcher(补料/新颖性)+ reviewer(plan_review 选题产物审查) | `[searcher, reviewer]` | read/write/edit + rag_retrieve + spawn + 4 引用工具(自产自写) |
| `reviewer` | 叶子审稿:笔记审稿 / 下载门禁 / 研究选题产物审查(plan_review)三种模式 | `[]` | 只读 + submit_review / submit_download_review + 溯源核验(list_citations/lookup_citation) |
| `qa-agent` | 回答论文/笔记/阅读记忆问题 | `[]` | rag_retrieve + 只读文件工具 + ask_user + analyze_figures + 记忆 7 项（对话检索、`reference_findings` 沉淀、清单/历史） |

`allowed_agents` / `allowed_spawns` 已由 spawn 工具在运行时强制（见 Orchestration）。记忆工具经 `get_memory_tools()` 装配后**按角色分发**（谁干活谁记录）：supervisor 7 个（blocks/ 核心块编辑 + `conversation_search`），searcher/noter 各 2 个（清单/历史写入），qa-agent 7 个（查询 + `reference_findings` 沉淀 + 清单/历史），researcher/reviewer 不装——记忆写入在干活者处记录，supervisor 不直接执行清单操作。

### Agent and ReAct loop

`Agent(llm, agent_registry, agent_type)` uses pull mode — it calls `agent_registry.get_config(agent_type)` internally to load tools and system prompt. Agent 的完整可注入依赖（`__init__` 参数）:

- `security_middleware` — 安全中间件列表（before/after/on_finish 洋葱模型，见 Security）
- `confirm_callback` — 确认回调；默认 fail-safe 拒绝（`_default_confirm` → `False`）
- `intent_enabled` / `intent_pipeline` / `conversation` — 意图识别（仅 CLI 构造的 supervisor 置 True；spawn 的子 agent 不传 → 门控关闭）
- `ask_user_callback` — ask_user_question 工具的消费回调（None 时该工具返回 fail-safe 提示）
- `session_id` — 跨多轮 run 的会话标识；与 CLI 的 AgentManager.create_agent id 必须一致（记忆/Sleeptime 按它键控）
- 记忆服务句柄：`memory` / `agent_manager` / `block_manager` / `message_manager` / `compaction` / `structured`（None 时相关路径零开销跳过）
- `stream_callback` — 流式事件回调（CLI 渲染器消费）；None = 非流式路径（`run()` 保持调 `chat()`）

`Agent.run(task) -> str` 是 async ReAct 循环:

1. 构造 head：① AGENT.md(system_prompt) ② SKILLS 清单（若有）③ `Memory.compile()`（仅渲染 `system/` 块 assistant/profile + 文件树索引，渐进暴露）④ INTENT 块（intent_enabled 且管线成功时）；末尾 user task。**澄清早退**：管线产出 clarification 且非 force_dispatch → 直接返回澄清文本（不落盘、不进 ReAct，澄清只在 CLI 层跨轮处理）
2. 从 MessageManager 加载该会话 in-context 消息（跨轮回放，Letta 语义）；当前 user task 落盘
3. 调 LLM 前检查压缩（`should_compress` → `run_compaction`，只改 in-context 窗口不删 SQL）；随后 `chat()` 或 `chat_stream()`（挂 stream_callback 才走流式）
4. 无 tool_calls → 顺序执行各中间件 `on_finish` 钩子（可改写最终回答）→ 落盘 → 返回。**截断续写**：`finish_reason=="length"` 时暂存半截、把「半截 + 续写提示」放回 in-context 继续循环，绝不把残缺内容当最终回答交付
5. 有 tool_calls → 并发执行（`asyncio.gather` + 信号量上限 4 + 确认锁串行，结果按调用顺序返回），tool 结果以 `role="tool"` 消息落盘 + 附加 in-context
6. 超过 `max_turns`（默认 20）→ 抛 `MaxTurnsExceeded`（唯一向上抛的错误——LLM 陷入无法自主退出的循环，调用方须介入）

`_exec_tool` 的中间件管道：构造 ToolContext → 解析 JSON 参数（失败走 after 链）→ 未知工具检查 → before 钩子（可抛 `ConfirmRequired`/`SecurityError`，被捕获转为带 `summary.decision` 的 ToolResult：`policy_denied`/`user_denied`/`auto_denied`/`security_blocked`）→ 执行工具（`asyncio.to_thread`，异常转错误 ToolResult）→ 逆序 after 钩子。所有错误都「降级为文本」反馈给 LLM，由 LLM 决定重试/调整/放弃。

记忆会话内即时生效：`_refresh_head_memory` 每轮开头重建记忆块并替换 head——同轮里 `memory_replace` 改的块，下一轮 LLM 调用即见。

### LLM client

`LLMClient(config: LLMConfig)` wraps the `openai` SDK as an async client via `asyncio.to_thread`. Two modes:
- `chat(messages, tools=..., ...)` — 非流式，ReAct 主路径；StructuredOutput 等 JSON 抽取也用
- `chat_stream(messages, tools=..., on_delta=...)` — 流式，仅 CLI 实时渲染用；`on_delta` 跑在线程池线程内，只能追加/打印，别碰事件循环

`Message` is a dataclass (`role`, `content`, `tool_calls`, `tool_call_id`, `truncated`) that serializes to OpenAI wire format (`_message_to_openai`，出站时清洗未配对 surrogate)。`tool_to_openai_schema(t) -> dict` converts a Tool to the OpenAI function-calling JSON Schema.

关键行为：
- **参数降级重试**：端点不支持 `response_format` / `extra_body` / `stream_options` 时（不同兼容端点措辞各异），去掉该参数重试一次（`_looks_like_unsupported_param`）
- **telemetry_callback**：LLM 调用元数据回调（model/tokens/duration_ms/finish_reason，**不含消息正文**），供审计 replay；None 时零开销跳过
- api_key 留空时 fail-fast 抛「LLM API key 未配置」，给出可行动配置指引

### Security middleware

安全模型在 `paperflow/core/security/`（`base.py` 定义协议，`middleware/` 放具体中间件，顶层 `__init__.py` 集中导出；`security.py` 文件已不存在——包与同名模块共存时包优先导入，避免死代码）。

协议层：`ToolContext`（trace_id/session_id/agent_type/tool/args/…，审计与决策的载体）+ `SecurityMiddleware` ABC（`before`/`after`/`on_finish`/`on_approval`/`record_llm_call`）。异常体系：`PolicyDenied` / `SecurityBlocked` / `ConfirmRequired`，都继承 `SecurityError`。

CLI 装配的 4 个中间件（`cli.py`，顺序即执行顺序）：

1. **AuditMiddleware** — 每次工具调用 + LLM 调用落 SQLite 审计（含 approval requested/decided 两条独立事件、`record_llm_call` 元数据）。after 钩子失败不中断结果返回
2. **WorkspacePolicyMiddleware** — 路径边界：校验 `format="path"` 参数为绝对路径（相对路径直接拒绝），敏感路径黑名单硬拦截（workspace/audit、workspace/milvus、`.git`/`.claude`/`.zcode`、`config.yaml`/`.env`、凭证与 shell 配置文件、`/etc` 等系统目录前缀）。白名单机制已退役：path 工具统一「任意绝对路径+黑名单」，默认写根由 make_tools 按 agent 装配注入。
3. **SecurityScanMiddleware** — 工具输出扫描（`output_scan="mark"` 的工具标注关键内容）
4. **PolicyEngineMiddleware** — 三级检查：`blocked_by_default` 直接拒；`risk_level` 超过会话阈值 `max_risk`（默认 "medium"）拒；`requires_confirm` 抛 `ConfirmRequired` → 用户确认后同一（工具名, 目标路径）不再重复询问

`Tool` 安全元数据（`paperflow/core/tool.py`）：`risk_level`（low/medium/high/critical）、`side_effects`、`blocked_by_default`、`requires_confirm`、`output_scan`（"mark"/None）、`root_hints`（语义根名提示 → `make_tools` 生成 `[目录]` 提示，不参与强制；强制=绝对路径+黑名单）。注册表加载时校验这些字段的合法值。

### Memory system

`paperflow/core/memory/` 是 **Letta 记忆栈的忠实移植**（取代旧的文件式 MemoryStore/GitStore/Dream）。分层：

- `schemas/` — pydantic 数据模型（`Block`/`Memory`/`Message`/`AgentState`）
- `orm/` — SQLite 持久化：`MemoryDB`（stdlib sqlite3 单例，`threading.Lock` 包裹写事务，`check_same_thread=False`）；表：blocks/block_history/messages/agent_state
- `services/` — 业务层管理器
- `tools/` — 11 个 LLM 面记忆工具（一工具一文件，3 组）
- 顶层 — `compaction.py`（上下文压缩）、`sleeptime.py`（后台记忆整合）、`runtime_context.py`（运行时上下文）

**核心服务**（装配顺序即依赖方向，见 `cli.py`）：

| 服务 | 角色 |
|---|---|
| `BlockManager` / `GitEnabledBlockManager` | 核心记忆块 CRUD。乐观锁（`version` 递增）+ 写前 `block_history` 快照（undo/redo）；`read_only` 块拒绝读写、块长上限 2000；`ensure_default_blocks()` 播种 assistant/profile（幂等，不覆盖用户已编辑块）。Git 变体每次变更同步 MemFS markdown 投影并 git commit |
| `MessageManager` | 对话全量落盘（Recall）。`get_in_context_messages()` 按 `AgentState.message_ids` 回放窗口；`make_ask_recorder()` 把子 agent 的 ask 问答也落盘 |
| `AgentManager` | Agent 生命周期：`AgentState` JSON 行（keyed by agent_id；message_ids = in-context 窗口） |
| `MemFS` | Git 托管的 markdown 投影层：`system/assistant.md` + `system/profile.md` + 其他块；自动生成 `memory_filesystem.md` 索引；`detect_file_changes()` 检测手工编辑回写块（双向同步） |
| `TitleExtractor` | 论文标题权威提取：5 级回退链（搜索元数据 > GROBID > LLM > pdftitle > PyMuPDF 启发式），**绝不回退到 PDF 文件名** |

**关键不变式**：
- **SQL 块是真相源，markdown 是投影**——与旧 GitStore 的语义正好相反
- 记忆工具在 **`paperflow/tools/memory/`**，经 **`get_memory_tools()`**（`tools/memory/__init__.py`）惰性构建 11 个工具（模块级单例，双重检查加锁，每次返回新列表副本）；执行时经 **`set_memory_context(MemoryToolsContext(...))`** 绑定一次（cli.py）+ `get_memory_context()` 取运行时上下文；未装配时工具降级为错误文本而非崩溃
- 11 个记忆工具分 3 组：**blocks**（`memory`/`memory_replace`/`memory_insert`/`memory_rethink`/`memory_apply_patch`/`memory_finish_edits`）、**recall**（`conversation_search`，默认过滤 tool 消息防递归噪音）、**paper_lists**（`unread_list_add`/`unread_list_remove`/`history_append`/`extract_title`——列表块工具，`unread_list_add` 要求真实标题绝不用文件名）
- **Compaction**（`compaction.py`）：只压缩 in-context 窗口（驱逐旧对话 + 插 SummarySchema 摘要 + 保留尾部），**永不删 SQL 行**；`should_compress`（tiktoken 估算，超 `trigger_ratio × context_size` 触发）+ `run_compaction`（滑动窗口，保留 tool 消息与其结果的配对，尾部孤儿清理）
- **Sleeptime**（`sleeptime.py`）：后台记忆整合，REPL 每轮循环顶部 `run_once_if_due()`（读 stdin 前）；LLM 产出 `MemoryEditBatch` 经 BlockManager 应用 + git commit；两阶段校验（类型枚举白名单：system/ 精确枚举 profile/assistant、顶层仅 feedback_/project_/reference_ 三前缀；动作仅 append/replace，delete 全量收禁），连续 3 次失败强制推进游标防死循环
- 装配不变式：CLI `session_id` == `AgentManager.create_agent` id == `Agent.session_id`，三者错位会各自读到空数据

### Intent recognition

`paperflow/core/intent/` — 意图识别框架，**仅 CLI 构造的 supervisor 装配**（子 agent 门控关闭，省 LLM 调用）。`IntentPipeline` 4 级级联，前一级未裁决才进下一级：

1. **实体抽取**（`routing/entities.py`）— 确定性正则，抽 pdf_path/arxiv_id/doi/note_path/figure（只抽实体不判意图）
2. **选项答复检测**（`routing/option_reply.py`）— 确定性正则识别纯编号菜单选择（`1`/`1.`/`选项2`/`第3个`）；命中直接产出 `MENU_SELECTION`（confidence=1.0），**不经路由/LLM 重分类**——对齐 Rasa 按钮 payload 惯例：选择动作的语义由发菜单的一方（supervisor 对照上轮菜单）承载，避免 0 阈值路由以微小分数误命中任意意图后误拦派发
3. **追问判别**（`routing/followup.py`）— 词表启发式（那/这/呢/然后 + 无动词无数量词）；命中则继承上一轮意图并合并实体
4. **混合路由**（`HybridRouter`）— 稠密（千问嵌入）+ 稀疏（jieba BM25）融合（`dense × alpha + sparse × (1-alpha)`，生产 `alpha=0.5`（2026-09-05 标定实验选定：seed 固定后 0.3-0.6 实测 0.793/0.824/0.831/0.716））；`load_routes()` 读 `data/intents/routes.yaml`（唯一知识库源，含各意图示例句 + 标定阈值）；命中阈值则产出
5. **LLM 兜底** — 无路由命中时注入 top-3 近邻候选，经 `StructuredOutput` 分类，最终兜底 `IntentionResult(GENERAL, 0.0)`；提示词交代 `clarification` 的填写条件（指代/动作不明才填，能推断则留空用 confidence 表达不确定），该字段的 pydantic `description` 随 schema 展开进 system 消息——两处都给模型交代过条件，它才会产出澄清

产出 `IntentOutput`（intent_type/confidence/entities/rewritten_query/source/steps/clarification）注入 ReAct head 的 `INTENT:` 块。`INTENT_META` 是意图元数据的**单一真相源**：15 个 `IntentType` 值分 3 类（business 业务派发 / dialogue 会话状态 / system 直接回答），`dispatch_allowed` 决定 spawn 门禁（chitchat/out_of_scope 等永远不能 spawn）。业务意图与子 agent 的对应：search_paper→searcher、generate_note→noter、ask_question/analyze_paper/manage_memory→qa-agent、research_discovery→researcher（选题发现）；`menu_selection`（选项答复，对话管理可派发）由 supervisor 对照上轮菜单转换成对应动作/派发，无法对应先 ask_user 确认。

跨轮澄清：`IntentPipeline` 产出 `clarification` → Agent 早退返回问题（不落盘）→ CLI `ConversationState.pending_intent` 挂起、下一轮合并重跑；`round >= 2` 超轮终止（force_dispatch 强制调度，绝不重跑后再次挂起）。`prev_intent`/`prev_user_input` 供追问判别。触发侧是**提示词层契约**（是否该问由 LLM 依提示词判断），轮数上限是**代码层硬约束**（`_merge_pending` 的 `round >= 2` 逃逸 + runtime 的 `force_dispatch` 旁路）。

### RAG

`paperflow/rag/` — 检索增强栈，`RAGService` 是唯一门面（indexer 与 retriever 是同一实例的两个视图，共享一把锁，增量写入对查询立即可见）。**懒加载单例**：`get_rag_service(config=None)`（双重检查加锁），所有重量组件（embedder/reranker/grobid/vector_store/bm25）首次访问才构造——`rag/__init__.py` 因此在包导入期不拉重型依赖。

端到端链路：**解析**（`GrobidClient` HTTP 解析 TEI XML → `ParsedDoc`；GROBID 不可达时回退 `PyMuPDFParser` 字体启发式分节；按 (path, mtime, size) 缓存）→ **分块**（`AcademicChunker` 两段式：按节 → 长节按 token 512/overlap 64 重切，跳过参考文献；Chunk id = sha1(path:index) 幂等）→ **索引**（`RagIndexer` 增量扫描，state 文件 `index_state.json`；文档级「删旧建新」，Milvus upsert + BM25 同步；含一致性恢复）→ **检索**（`Retriever` 混合：BM25 top-30 + 向量 top-30 → RRF 融合 → `SbertReranker` 重排 → 有序 Chunks）。

存储与模型：
- `VectorStore` — Milvus（`pymilvus.MilvusClient`，单 collection `config.milvus_collection`="paperflow"）；`config.milvus_uri` 默认 `http://localhost:19530` 连 Standalone（`docker compose up -d` 起 etcd+minio+milvus，gRPC 19530 / 健康检查 9091，数据落 `data/milvus/`）；传本地文件路径则走 Milvus Lite 内嵌（单测用，无需常驻服务）
- `Bm25Index` — rank_bm25 + jieba；是向量库文本的**投影**，启动时从 `store.all_documents()` 重建
- `SbertEmbedder` — `Qwen/Qwen3-Embedding-0.6B`（1024 维，CPU，L2 归一化，维度从模型读取）；`SbertReranker` — `Qwen/Qwen3-Reranker-0.6B` CrossEncoder（sentence-transformers≥5.4 原生包装，sigmoid 打分）
- 加载路径 `resolve_model_dir(workspace, model_name)`：本地优先（`<workspace>/models/<name>/` 存在用本地），否则回退 HF 名自动下载

消费方：`RagRetrieveTool`（`tools/rag/`，`rag_retrieve`）装配进 qa-agent 与 researcher（researcher 用它按课题盘点语料）；`ReadPdfTool` 用 `parse_pdf_cached`；`write_file`/`edit_file`/`fetch_pdf` 写盘后自动触发 `index_document`。embedder 单例与意图管线共享（`cli.py` `_rag_embedder`；记忆检索为纯 SQL LIKE，不用向量）。

### Citations

`paperflow/citations/` — 引用管理（溯源落地）。`references.bib` 是引用库**真相源**：append-only 追加、绝不重写（用户手工维护的分节注释原样保留）。`bib.py` 轻量扫描条目（查找/去重）；`corpus.py` 是「语料里有哪些论文」的易变投影（note H1 + PDF 解析标题 → 全标题精确匹配，按 (path, mtime_ns) 增量重建）；`manager.py` 编排：引用解析（干净全标题/路径 → key+status）、入库（语料内 PDF / 库外 EXTERNAL）、去重、渲染（author-year/numbered/bibtex/gbt7714）、调和（渲染视图回填空字段，bib 文件不动）。懒加载单例 `get_citation_manager()`，重组件（corpus 索引、TitleExtractor）首次使用才构造。

4 个引用工具（`tools/citations/`）装配给 **noter** 与 **researcher**（researcher 自产自写：survey/gaps/idea 卡/研究计划的溯源标注与参考文献渲染）；**reviewer** 装配 `list_citations`+`lookup_citation` 做溯源核验（核验 `[来源:key§节]` 的 key 真实存在于 references.bib，不信任标注本身）。

产物溯源标注：
- **笔记**头部写 `**论文引用**: [key]`（落盘前经 `lookup_citation` 确认 key 真实性），各节关键论断标节级 `[来源:§X]`，供 reviewer 沿链回溯核对原文

### Tools

`paperflow/tools/` — 原子工具，一工具一文件，按域分包；`paperflow/tools/__init__.py` 再导出全部 13 个工具供消费方统一导入（导出符号名稳定，内部路径随便拆）：

- `file/` — 读/写/编辑/glob/grep/read_pdf/format_check（+ `atomic.py` 原子写盘）
- `search/` — `web_search`（按 source 搜：arxiv/openalex/semantic_scholar，`_SOURCE_REGISTRY` 注册；单源一次调用，多源由 searcher 并行多次调、结果自动去重入池；semantic_scholar 走 `PAPERFLOW_S2_API_KEY`，缺 key 用公共端点，源失败沿熔断降级）、`fetch_pdf`（下载）；`clients/` 是纯 API 客户端（共享 `_HttpClientMixin` SSRF 校验 + 逐跳重定向校验）；`_common.py` 有 `SearchRunState` 跨调用去重池（`wants_run_state` opt-in）、查询 LRU 缓存、源级熔断器
- `review/` — `submit_review` / `submit_download_review`（reviewer 的裁决工具）
- `rank/` — `lookup_venue_rank`（期刊/会议等级查询）
- `citations/` — 4 引用工具（`lookup_citation`/`add_citation`/`format_citations`/`list_citations`，装配 noter 与 researcher；reviewer 装 list+lookup 溯源核验）
- `rag/` — `rag_retrieve`（`RagRetrieveTool`：惰性取 RAGService 单例 + 持锁检索 + 格式化结果；装配 qa-agent 与 researcher）
- `vision/` — `analyze_figures`（`needs_parent=True`：视觉 LLM 调用归属父 agent 轮次进审计）。图提取走 pdffigures2 管线（proposal 候选 + 打分选优 + no-overlap 互斥），随后视觉模型结构化看图分析 + 嵌入落盘；key 缺失/无图/失败全降级
- `memory/` — 11 个记忆工具（`get_memory_tools()` 惰性单例 + `set_memory_context`/`get_memory_context` 运行时上下文；blocks/recall/paper_lists 三组；装配 supervisor，子 agent 各装子集）
- `orchestration/` — `spawn_sub_agent` / `ask_user_question` / `SubAgentMode`（见下）
- `common/` — `make_tools(config, tool_items, default_write_root=None)` 装配工厂：按 `root_hints` 生成 `[目录] {root}={path}` 提示（scratch 根对 LLM 不透明）、`default_write_root` 盖章到 write_file（noter→note、researcher→research）、注入 `_config`；`_http.py` 共享 HTTP 基础设施

根映射（`_root_map`）：note→`note_dir`、pdf→`pdf_dir`、research→`research_dir` 或 `workspace/research`、memory→`workspace/memory`、templates→`workspace/templates`、scratch→`workspace/tmp`。

### Orchestration

`paperflow/tools/orchestration/spawn.py` — **SpawnSubAgentTool**（`spawn_sub_agent`，`needs_parent=True`）。`execute(agent_type, task, mode=None)`：

1. **模式校验**：未知 `mode` → denied
2. **意图派发门禁**：父 agent 的意图 `dispatch_allowed=False`（chitchat/out_of_scope/help/switch_topic 等）→ 永不 spawn
3. **spawn 权限**：`_check_spawn_allowed` — supervisor 硬编码放行；其余 agent 查自己的 `AgentConfig.allowed_spawns` 白名单
4. **去重注册表**（`_SPAWN_REGISTRY`，按 session_id + 任务指纹）：**无路径任务** 运行中去重 + 完成结果 300s 内可复用；**含路径任务** 只做运行中去重（文件可能中途变化，完成不缓存）
5. **审稿预算**：同一父 run 内 note_review/download_review/plan_review spawn ≤3 次,超限 denied(轮数预算下沉代码,LLM 不数轮次)
6. **子 agent 构造**：继承父的 security_middleware / session_id / confirm_callback / ask_user_callback（子 agent 能中途问用户）；**不传**意图管线/会话（子任务是结构化任务非用户意图）；`mode` 经「当前模式：{mode}」注入 system prompt
7. **预算执行**：超时 = 基座超时（`config.agent_timeouts`，audit 数据校准:noter 900s/searcher 420s/reviewer 300s/researcher 1800s/qa-agent 180s,2026-09-05）+ 累计用户等待（`_UserWaitClock` 同时排除 confirm 确认与 ask_user 提问的人工等待）；`asyncio.TimeoutError`→timeout、`PermissionError`→denied、其他异常→failed
8. **摘要提取**：末尾 2000 字符经 `StructuredOutput` 抽结构化 `digest`（按 agent_type 选 `SearcherDigest`/`ReviewerDigest`/`NoterDigest`/`ResearcherDigest`/`GenericDigest`），失败回退全文摘要

返回 `ToolResult(text=SubAgentResult.model_dump_json(), summary=model_dump())`。`SubAgentResult.status` ∈ {success, failed, timeout, denied}，`needs_attention=True` 表示「被拒且需用户介入」。只有 supervisor（和需要 reviewer/searcher 的 searcher/noter/researcher）装配此工具——权限最小化：叶子 agent 不递归。

**AskUserQuestionTool**（`ask_user_question`，`needs_parent=True`）：读 `parent.ask_user_callback`（CLI 注入，worker 线程读 stdin）；回调为 None 时 fail-safe 返回「无法交互，请基于已有信息决定」，绝不挂起。装配权限在装配层（supervisor/searcher/noter/qa-agent/researcher 有，reviewer 无）。

### Terminal

`paperflow/terminal/` — 终端交互隔离层，测试可注入。

- `io.py`：`InputIO` 契约（`read`/`confirm`/`ask`）。`PromptToolkitIO`（TTY，multiline + 历史；confirm 仅 y/n 键入，Enter 默认 No）vs `FallbackIO`（非 TTY，内置 `input()`）。`make_input_io(config)` 按 `stdin.isatty()` 二选一。`_confirm_lock` 串行化并行子 agent 的并发 confirm/ask（prompt_toolkit 会话非线程安全）
- `render.py`：`StreamRenderer`（线程安全）经 `on_event` 消费 `StreamEvent`（content/tool）。TTY = `RichBlock`（rich Live + spinner，0.08s 节流重绘）；非 TTY = `PlainBlock`（增量追加）。工具行 `[{agent_type}] Calling ...`，写/编辑工具完成后发 File written/edited 完成行；`should_print` 去重已流式展示的 root 内容；`suspend()` 在确认框/输入框前停 live（rich Live 与 prompt_toolkit 并发互相干扰——实测坑）
- `diff.py`：`compute_diff`（unified diff ±3）+ `truncate_diff`（≤200 行）——写/编辑确认前渲染 diff 预览
- 启动横幅：Codex 风格方框（`>_ paperFlow Academic Assistant` + model/workspace + Tip），无 emoji/版本/标语

### Config

`PaperFlowConfig.from_env()` loads in priority order: environment variables (`PAPERFLOW_*`) > `config.yaml` > dataclass defaults (DeepSeek endpoint, `deepseek-v4-flash` model). Key fields:

| 字段 | 说明 |
|---|---|
| `llm` (`LLMConfig`) | base_url / api_key / model / max_tokens(393216，给足防长草稿截断) / temperature(0.0) / context_window(1M) |
| `vision` (`VisionLLMConfig`) | 视觉模型（多模态图表分析）：base_url / api_key / model；默认 DeepSeek 视觉（与文本 LLM 同一端点/key），可经 env 换 OpenAI 兼容端点；api_key 留空不崩启动，图表分析调用时降级不可用 |
| `workspace` | 运行时数据根（`data/`）：milvus/memory/intents/models/audit/templates 等 |
| `agents_dir` | 插件扫描目录，默认 `agents` |
| `max_risk` | 策略引擎风险阈值，默认 "medium" |
| `compaction` | `CompactionSettings`（惰性工厂避免 config→compaction→llm→config 循环导入） |
| `sleeptime_enable` / `sleeptime_agent_frequency` | 后台整合开关 / 每 N 条新消息检查一次（默认 50） |
| `note_dir` / `pdf_dir` / `research_dir` | 语料库数据源根（note/pdf/research，个人绝对路径，**无默认值**，须经 .env/config.yaml） |
| `grobid_endpoint` | GROBID 服务地址，默认 `http://localhost:8070` |
| `milvus_uri` / `milvus_collection` / `embed_model` / `rerank_model` | Milvus 地址（默认 `http://localhost:19530`）/ 集合名（默认 `paperflow`）/ 千问嵌入 / 重排模型 |
| `citations_bib_path` | references.bib 路径（引用库真相源）。默认 `workspace/citations/references.bib`，可指向任意论文项目目录；空则回退默认 |
| `agent_timeouts` | 子 agent 超时覆盖表（noter 900 / searcher 420 / reviewer 300 / researcher 1800 / qa-agent 180;audit 数据校准,见 spec 2026-09-05-agent-timeout-recalibration） |

环境变量：`PAPERFLOW_API_KEY` / `PAPERFLOW_BASE_URL` / `PAPERFLOW_MODEL` / `PAPERFLOW_VISION_BASE_URL` / `PAPERFLOW_VISION_API_KEY` / `PAPERFLOW_VISION_MODEL` / `PAPERFLOW_WORKSPACE` / `PAPERFLOW_AGENTS_DIR` / `PAPERFLOW_MAX_RISK` / `PAPERFLOW_NOTE_DIR` / `PAPERFLOW_PDF_DIR` / `PAPERFLOW_RESEARCH_DIR` / `PAPERFLOW_GROBID_ENDPOINT` / `PAPERFLOW_MILVUS_URI` / `PAPERFLOW_MILVUS_COLLECTION` / `PAPERFLOW_EMBED_MODEL` / `PAPERFLOW_RERANK_MODEL` / `PAPERFLOW_SLEEPTIME_ENABLE` / `PAPERFLOW_SLEEPTIME_FREQUENCY` / `PAPERFLOW_CITATIONS_BIB_PATH` / `PAPERFLOW_S2_API_KEY`（Semantic Scholar 检索的可选 key，由 search 客户端直读环境变量，配置后走高配额端点）。env 恒为字符串，按目标字段当前类型做 bool/int 转换。

### Key design decisions

- **No deterministic pipeline.** Everything — routing, tool selection, task decomposition — is driven by the LLM's ReAct loop. Tools are just JSON Schema definitions fed to the model
- **`ToolResult.summary: dict`** (default empty) — 结构化摘要通道：决策结果（policy_denied/user_denied）、spawn digest、记忆工具结构化数据都经它承载
- **`risk_level` 已强制**：PolicyEngineMiddleware 按 `max_risk` 阈值拦截 + `requires_confirm` 确认（键 = (工具名, 目标路径)）；Tool 安全元数据由注册表加载时校验
- **`allowed_agents` / `allowed_spawns` 已强制**：spawn 工具运行时校验白名单 + 意图派发门禁（supervisor 硬编码放行）
- **安全是中间件洋葱**：before（可拒绝/要求确认）→ 执行 → 逆序 after；每轮 run 结束 on_finish 可改写最终回答。所有拦截降级为 ToolResult 文本，只有 `MaxTurnsExceeded` 向上抛
- **SQL 是记忆真相源，markdown 是投影**；压缩/窗口驱逐永不删 SQL 行（Recall 完整）；记忆工具按角色分发（supervisor 7 个、子 agent 按「谁干活谁记录」各装子集，权限最小化）
- **`Agent.run()` 返回 str**；子 agent 结果经 `SubAgentResult`（status/summary/digest/needs_attention）结构化回传 supervisor
- **流式零开销**：`stream_callback`/`telemetry_callback` 为 None 时全链路保持原非流式行为（mock/无 UI 调用方不受影响）
- **意图只进根 agent**：spawn 的子 agent 门控关闭；澄清只在 CLI 层跨轮处理，不暴露给 supervisor（避免 ask_user 双问）
- **契约式 prompt,两层装配**:编排决策 LLM 运行时自主(AGENT.md 只写契约+启发式);硬不变式下沉代码——诚实性协议在 BASE_PROMPT、审稿轮数预算在 spawn 五道闸
