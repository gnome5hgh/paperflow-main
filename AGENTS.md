# AGENTS.md

本文件是 `paperFlow` 仓库的总 manifest，对所有 AI 编码代理（ZCode / Claude Code / Codex 等）生效。

## Scope

- 作用范围覆盖整个 `paperFlow` 仓库；本文件是根级治理 manifest，与子目录 manifest 构成两级路由。
- **就近优先**：进入更深层目录后，优先服从距离更近的 `AGENTS.md`（当前有：`paperflow/` 及其 `core/` `rag/` `tools/` `citations/` `vision/` `terminal/`、`agents/`、`.paperflow/`）；根 manifest 负责「如何进入」，不替代子目录内部说明。
- 事实优先级：源码与测试 > 本 manifest 及各级子 manifest > `docs/` 设计文档。manifest 是入口，不是终点——文档与代码冲突时以代码为准。

## 目录结构

```text
paperFlow/
├─ agents/            # Agent 插件：<name>/AGENT.md(frontmatter+system_prompt) + tools.py（有 AGENTS.md）
├─ .paperflow/        # 用户级数据：skills/ 单级 Skill 插件（有 AGENTS.md）
├─ paperflow/         # 主包源码：core/ rag/ citations/ vision/ tools/ terminal/（包及其 core/rag/tools 有 AGENTS.md）
├─ docs/              # 设计文档（gitignored 本地文档）：adr/ learning/ superpowers/(spec/plan) CONTEXT.md
├─ scripts/           # 实验与标定脚本（gitignored）：intent/ rag/ 等
├─ tests/             # 测试套件（gitignored）
├─ data/              # 运行时数据根（按模块分目录）：intent/ templates/ 入库，其余运行期产物
├─ config.yaml        # 本地配置（gitignored），示例见 config.example.yaml
├─ AGENTS.md          # 本文件：根级治理 manifest（入库）
└─ CLAUDE.md          # Claude Code 入口薄指针（本地，不入库）
```

- `docs/`、`scripts/`、`tests/` 不入库是既定策略；其内容仍是排查与设计的一手资料，但运行期产物与个人路径不得入库。
- 运行期目录（`data/` 下非反选子目录、`.venv/`、`__pycache__/` 等）不作为事实源，除非用户明确要求排查运行时产物。

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
# GROBID 首次需先一次性初始化（复制 grobid-home 到 data/infra/grobid/），见 docker-compose.yml 首部注释
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

**API key 配置**：key 经 `config.yaml` 的 `llm.api_key`（配置文件 gitignored，可参考仓库根 `config.example.yaml` 复制为 `config.yaml`）或环境变量 `PAPERFLOW_LLM_API_KEY` 提供，**不硬编码在代码里**。未配置时启动即报「LLM API key 未配置」。RAG 嵌入/精排的 key 独立经 `rag.embedding.api_key` / `PAPERFLOW_RAG_EMBEDDING_API_KEY` 提供（留空则路由退纯 BM25、检索跳稠密路）。

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
  - **编号不回收、空缺须补齐**：删除或合并 ADR 后重编号（顺延补齐空位），并同步更新所有活文档引用（本文件设计文档索引、CONTEXT.md、docs/learning、修复清单）；历史 spec/plan 中的旧编号是历史记录，不回改
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

paperFlow 是 LLM 驱动的学术研究流程助手（ADR 0003）。单根 agent（supervisor）接收每一轮用户输入 → 意图识别（INTENT 块注入）→ ReAct 循环 → 拆解子任务 spawn 子 agent（searcher/noter/reviewer/qa-agent/researcher/librarian）→ 聚合各子 agent 的结构化摘要（digest）→ 汇总回答。

代码分层（自底向上）:

```
paperflow/
  core/          核心运行层:agent(ReAct 循环) + agent_registry + llm + tool(抽象)
                 + security(安全中间件) + memory(记忆系统) + intent(意图识别)
                 + structured(结构化输出) + mcp(MCP 客户端平台:后台循环 + 工具桥接)
  rag/           RAG 检索栈(解析/分块/向量/混合检索),懒加载单例
  citations/     引用管理(溯源落地):bib.py 读写 + corpus.py 语料标题索引
                 + manager.py 编排
  vision/        视觉分析(pdffigures2 提取管线: parsers/ 解析 + detectors/ 图检测 + 编排 + 视觉模型看图)
  tools/         原子工具:file/ search/ review/ rank/ orchestration/ citations/ rag/ vision/ memory/ common
  terminal/      终端交互:InputIO(输入) + StreamRenderer(渲染) + diff
agents/<name>/   Agent 插件:AGENT.md(frontmatter+system_prompt) + tools.py(TOOLS 列表)
.paperflow/skills/<name>/  Skill 插件:SKILL.md(agentskills.io 格式) + 可选 tools.py/references/
```

设计文档索引（ADR 按模块划分，正文恒为最新设计，修正史见 docs/adr/修正记录.md）：0001(RAG 管线)、0002(安全设计)、0003(ReAct 多 Agent 架构)、0004(记忆系统)、0005(SubAgent×Tool 映射)、0006(结构化输出)、0007(意图识别)、0008(引用管理)、0009(图片提取与多模态)、0010(终端与 CLI 入口)、0011(Skill 系统)、0012(MCP 客户端平台)。

### Agent plugin system

Every agent lives in `agents/<name>/` with two files:
- `AGENT.md` — YAML frontmatter (`name`, `description`, `allowed_agents`, `allowed_spawns`) + Markdown body(契约式结构:派发类 worker 为五段式——身份/边界/能力/交付契约/方法启发式;reviewer/qa-agent 按角色裁剪。编排决策由 LLM 运行时自主,不写跨 agent 编排序列)
- `tools.py` — module-level `TOOLS: list[Tool]` list. Each Tool is a subclass of `Tool` ABC with `name`, `description`, `parameters` (JSON Schema for OpenAI function calling), and `execute(**kwargs) -> ToolResult`

`AgentRegistry(agents_dir)` scans this directory at init time, parses frontmatter, dynamically imports `TOOLS` from each `tools.py`, and exposes `get_config(agent_type) -> AgentConfig` plus `list_agents()`. It is the single entry point for agent plugin discovery — the Skill system keeps a parallel registry (`SkillRegistry`, see below), which registers installed skills rather than agents.

装配时 `Agent.__init__` 在角色定义后拼接全 agent 共有的行为基座 `BASE_PROMPT`(`core/agent/base_prompt.py`:诚实性协议/交付契约语义/协作语义)——通用铁律不重复写在各 AGENT.md。

**Skill 体系**（`paperflow/core/skills/registry.py`）：Skill 是给**现有** agent 注入领域知识/流程指令/轻量工具的可安装能力包（agentskills.io 格式），无独立推理循环——与上面 agent 插件机制是平行而非同一概念。`SkillRegistry(skills_dir)` 单级扫描 `<项目根>/.paperflow/skills/`（内置与用户安装同层，`/skill install` 准入通道（REPL 内）或手动拷贝，版本对齐经集中 lock 文件），三级渐进披露：L1 `<available_skills>` name+description 清单注入 head（无 skill 零开销）→ L2 `load_skill` 工具按需加载正文 → L3 `load_skill(resource=...)` 读资源（路径围栏限 skill 目录内）。skill 捆绑的 `tools.py` 经 `merge_tools` 并入子 agent 工具表——supervisor 代码级恒不并入（权限最小化红线）；含代码的安装强制人工过目（`-y` 拒绝，须显式 `--allow-code`）。

**MCP 客户端平台**（`paperflow/core/mcp/`，ADR 0012）：config.yaml 顶层 `mcp_servers` 声明的任意 MCP server，其工具经 `McpClientManager`（自持一条后台事件循环线程，每 server 一条持久 `ClientSession`，全部活在后台循环里）发现、经 `bridge.py` 逐工具桥接为原生 Tool（`mcp__<server>__<tool>`，schema 规范化 + allowed/disabled/风险分级过滤），在 cli.py 装配循环经 `merge_tools` 第 4 组（`("mcp", …)`）注入 agent；连接失败的 server 跳过不挡启动，调用失败重连一次后以错误文本回传模型（绝不抛进 ReAct 循环）；REPL `/mcp` 命令看各 server 状态。执行链路：runtime 的 `asyncio.to_thread(tool.execute)` 工作线程 → 投递后台循环执行。

现有 7 个 agent（`agents/` 下）:

| agent | 职责 | allowed_spawns | 工具要点 |
|---|---|---|---|
| `supervisor` | 调度主管:拆解任务、spawn、汇总 | 硬编码放行所有（绕过白名单） | 仅 2 个调度工具 + 7 个记忆工具（blocks/ 核心块编辑 + `conversation_search`） |
| `searcher` | 多源搜索 → reviewer 门禁 → 可选下载 | `[reviewer]` | MCP 检索（mcp__paper-search__*） + fetch_pdf + ask_user + spawn |
| `noter` | 纯笔记生成：基于指定 PDF 起草结构化笔记,内部 reviewer 审稿 ≤3 轮 | `[reviewer]` | 原子文件工具 + ask_user + spawn + glob/grep + 4 引用工具(lookup/add/format/list) + analyze_figures |
| `researcher` | 选题发现:基于本地语料盘点→survey/gaps→idea 卡→外部新颖性验证(源优先 semantic scholar,失败如实标「未经外部验证」)→研究计划,产物自己落盘 research 根,内部 spawn searcher(补料/新颖性)+ reviewer(plan_review 选题产物审查) | `[searcher, reviewer]` | read/write/edit + rag_retrieve + spawn + 4 引用工具(自产自写) |
| `reviewer` | 叶子审稿:笔记审稿 / 下载门禁 / 研究选题产物审查(plan_review)三种模式 | `[]` | 只读 + submit_review / submit_download_review + 溯源核验(list_citations/lookup_citation) |
| `qa-agent` | 回答论文/笔记/阅读记忆问题 | `[]` | rag_retrieve + 只读文件工具 + ask_user + analyze_figures + 记忆 7 项（对话检索、`reference_findings` 沉淀、清单/历史） |
| `librarian` | 文献库维护:同步/新增/删除/查询导出 references.bib | `[]` | 6 引用工具（lookup/add/format/list + sync/remove 仅此 agent 装）+ read_pdf（元数据缺失时读首页取标题/作者，不属内容分析）+ ask_user |

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

1. 构造 head：① AGENT.md(system_prompt) ② SKILLS 清单（若有）③ `<available_agents>` 可派发子 agent 清单（按能力选型；非派发方为空串整块省略）④ `Memory.compile()`（仅渲染 `system/` 块 assistant/profile + 文件树索引，渐进暴露）⑤ INTENT 块（intent_enabled 且管线成功时）；末尾 user task。**澄清同步问**（2026-10-04 统一）：管线产出 clarification → runtime 在构建 head 时同步调 ask 回调问用户（routing.confirm 原语解析编号选择），答案代码级落地为意图（source=user）后本 run 直接以正确意图启动——无跨轮挂起、无 force_dispatch 通道
2. 从 MessageManager 加载该会话 in-context 消息（跨轮回放，Letta 语义）；当前 user task 落盘
3. 调 LLM 前检查压缩（`should_compress` → `run_compaction`，只改 in-context 窗口不删 SQL）；随后 `chat()` 或 `chat_stream()`（挂 stream_callback 才走流式）
4. 无 tool_calls → （识别到多意图时先注入一次收尾核对账本：把「识别到的意图 + 派发账本 + 新落盘产物」摆给模型自查，代码只摆账本不下结论）→ 顺序执行各中间件 `on_finish` 钩子（可改写最终回答）→ 落盘 → 返回。**截断续写**：`finish_reason=="length"` 时暂存半截、把「半截 + 续写提示」放回 in-context 继续循环，绝不把残缺内容当最终回答交付
5. 有 tool_calls → 并发执行（`asyncio.gather` + 信号量上限 4 + 确认锁串行，结果按调用顺序返回），tool 结果以 `role="tool"` 消息落盘 + 附加 in-context
6. 超过 `max_turns`（默认 20）→ 抛 `MaxTurnsExceeded`（唯一向上抛的错误——LLM 陷入无法自主退出的循环，调用方须介入）

`_exec_tool` 的中间件管道：构造 ToolContext → 解析 JSON 参数（失败走 after 链）→ 未知工具检查 → before 钩子（可抛 `ConfirmRequired`/`SecurityError`，被捕获转为带 `summary.decision` 的 ToolResult：`policy_denied`/`user_denied`/`auto_denied`/`security_blocked`）→ 执行工具（`asyncio.to_thread`，异常转错误 ToolResult）→ 逆序 after 钩子。所有错误都「降级为文本」反馈给 LLM，由 LLM 决定重试/调整/放弃。

记忆会话内即时生效：`_refresh_head_memory` 每轮开头重建记忆块并替换 head——同轮里 `memory_replace` 改的块，下一轮 LLM 调用即见。

运行期状态容器（`core/agent/state.py`）：把过去散在各模块、生命周期不一的模块级字典收成两个显式作用域——`SessionState`（跨 run 存活：同会话 spawn 去重注册表、同 agent_type 连续失败计数）与 `RunState`（按一次用户任务 / `trace_id` 隔离：搜索去重池与负缓存、supervisor 派发账本、审稿与每轮派发预算计数、在途写盘路径、产物账本）。两者都按 TTL 惰性清扫（取用时顺手剔除过期条目，不起定时任务；run 作用域按滑动窗口推进取用时刻，活跃任务不会被自身清扫误删）。

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
| `BlockManager` / `GitEnabledBlockManager` | 核心记忆块 CRUD。写入是**一次持锁的原子读-改-写 + 写入 CAS**（带期望版本的条件更新，版本被并发写者推进即拒绝而非静默覆盖）；**读-改-写类块写入（工具与 Sleeptime 的块更新）必须经 `mutate_block`**——追加/替换类操作把「读旧值→算新值→写回」整段放进一次持锁事务，避免两个并发写者各自基于同一份旧值计算后互相抹掉（CAS 冲突重读最新值重放 mutator）；整体设值的 `update_block_value` 是同款原子 + CAS 路径；写前 `block_history` 快照（undo/redo）；`read_only` 块拒绝读写、块长上限 2000；`ensure_default_blocks()` 播种 assistant/profile（幂等，不覆盖用户已编辑块）。Git 变体每次变更同步 MemFS markdown 投影并 git commit |
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

`paperflow/core/intent/` — 意图识别框架，**仅 CLI 构造的 supervisor 装配**（子 agent 门控关闭，省 LLM 调用）。`IntentPipeline` 五级级联，前一级未裁决才进下一级：

1. **实体抽取**（`routing/entities.py`）— 确定性正则，抽 pdf_path/arxiv_id/doi/note_path/figure（只抽实体不判意图）
2. **选项答复检测**（`routing/option_reply.py`）— 确定性正则识别纯编号菜单选择（`1`/`1.`/`选项2`/`第3个`）；命中直接产出 `MENU_SELECTION`（confidence=1.0），**不经路由/LLM 重分类**——对齐 Rasa 按钮 payload 惯例：选择动作的语义由发菜单的一方（supervisor 对照上轮菜单）承载，避免 0 阈值路由以微小分数误命中任意意图后误拦派发
3. **追问判别**（`routing/followup.py`）— 词表启发式（那/这/呢/然后 + 无动词无数量词）；命中则继承上一轮意图并合并实体
4. **混合路由**（`HybridRouter`）— 稠密（千问嵌入）+ 稀疏（jieba BM25）融合（真实融合 = `sim_d + (1-α)²·sim_s`——α 只衰减稀疏路、非凸组合，见 `scripts/intent/calibration/README.md`；生产 alpha 读 `config.intent.router.alpha`（唯一声明点 `paperflow/config.py`），**2026-10-05 阈值标定实验取 0.15**（CV 平台区覆盖整个扫描区间，α 不敏感））；`load_routes()` 读 `data/intent/routes.yaml`（唯一知识库源，含各意图示例句 + 标定阈值，阈值自 2026-10-05 起非零）；命中阈值则产出
5. **LLM 兜底** — 无路由命中时注入 top-3 近失候选，经 `StructuredOutput` 分类，解析失败/判定失败兜底 `IntentionResult(UNCLASSIFIED, 0.0)`（unclassified 是显式失败信号，路由层不建兜底路由）；提示词交代 `clarification` 的填写条件（指代/动作不明才填，能推断则留空用 confidence 表达不确定），该字段的 pydantic `description` 随 schema 展开进 system 消息——两处都给模型交代过条件，它才会产出澄清。复合拆分 `steps` 同理走 `Field(description)` 触发契约（≥2 个独立业务动作才填、≤3 步、steps[0]==intent_type），`_steps_guard` 三重护栏（业务白名单/≤3 步/首步一致）违规整体置空。`steps` 只是**信号**：识别到的意图随 INTENT 块注入，并在收尾时作为事实摆给 supervisor 自查；实际派发顺序与并行由 supervisor 自主决定，框架不强制（不再有按 steps 队列逐步骤校验的门禁）

阈值与常量标定（`scripts/intent/` 下，gitignored；每个实验目录自成 `goldens/`（题集）+ `results/`（存档与报告），题集**不放在 `data/intent/`**——那里只留生产知识库 `routes.yaml` 与路由向量缓存）：`scripts/intent/calibration/`（2026-10-05 完成：编码器模型 + BM25 k1/b/idf + top_k + alpha + 12 条路由阈值 + 判据三常量 + 拟合超参 + 结构常量的分层序贯标定，交付值已写回；报告 `results/report.md`）；`scripts/intent/eval/`（计划中：用指标反映模块可用性）。

产出 `IntentOutput`（intent_type/confidence/entities/rewritten_query/source/steps/clarification）注入 ReAct head 的 `INTENT:` 块。`INTENT_META` 是意图元数据的**单一真相源**：14 个 `IntentType` 值分 3 类（2026-10-01 收敛：switch_topic 并入 set_research_topic、refine_query 并入 search_paper）（business 业务派发 / dialogue 会话状态 / system 直接回答），`dispatch_allowed` 决定 spawn 门禁（chitchat/out_of_scope 等永远不能 spawn）。**意图是信号不是选型依据**：supervisor 读 system 里的 `<available_agents>` 清单（各 agent 的 description 即其职责与边界说明），按能力挑角色、自行决定顺序与并行，不按意图名对号入座；`menu_selection`（选项答复，对话管理可派发）由 supervisor 对照上轮菜单转换成对应动作/派发，无法对应先 ask_user 确认。

澄清（2026-10-04 统一为单通道）：触发权在代码（`_ambiguous` 的 S1 贴线/S2 竞争分数判据）→ runtime `_resolve_clarification` 同步调 ask 回调问用户（问题文本由强制澄清 LLM 调用生成、末尾代码追加编号选项行）→ `routing.confirm.match_option_choice` 解析回复，命中候选 → 合成 `source=USER` 的确认意图（跳过路由复判），未命中 → 答案附录进任务按最佳猜测继续（单次问答、无循环）。`prev_intent`/`prev_user_input` 供追问判别。agent 执行中途问用户走 `ask_user_question(intent_options=...)`——同一 confirm 原语、同一落地代码（父 agent 的 last_intent/prev_intent 立即更新）。spawn 门禁声明优先：显式声明的可派发意图即放行（会话意图误判时本轮唯一申诉通道），声明的不可派发意图明确拒绝。

### RAG

`paperflow/rag/` — 检索增强栈，`RAGService` 是唯一门面（indexer 与 retriever 是同一实例的两个视图，共享一把锁，增量写入对查询立即可见）。**懒加载单例**：`get_rag_service(config=None)`（双重检查加锁），所有重量组件（embedder/reranker/grobid/vector_store/bm25）首次访问才构造——`rag/__init__.py` 因此在包导入期不拉重型依赖。

端到端链路：**解析**（`GrobidClient` HTTP 解析 TEI XML → `ParsedDoc`；GROBID 不可达时回退 `PyMuPDFParser` 字体启发式分节；按 (path, mtime, size) 缓存）→ **分块**（`AcademicChunker`：按节切 → 句界装窗 512/overlap 64——不切句、重叠取上一窗尾完整句、单句超长回退 token 滑窗；块首行 `context_prefix` 逐窗拼「标题 > 章节」前缀（PDF=GROBID 主标题，笔记=首个 `# ` 行），切块前丢弃判据：参考文献 + 期刊样板章节（`_DROP_HEADS`：致谢/资助/利益冲突/数据可用性等，标题子串匹配）+ 解析残渣正文（<12 token 且无句末标点的微碎片/空正文）；Chunk id = sha1(path:index) 幂等）→ **索引**（`RagIndexer` 增量扫描，state 文件 `index_state.json` 带**配方哈希**（`_recipe_hash`，输入含 `RECIPE_LOGIC_REVISION` 与 chunker 的 max/overlap、indexer 的 table_text_limit、embedding 的 embed_model；指纹不符放弃旧状态全量重扫重嵌，改切块参数自动失效；GROBID 降级不纳入）；表格/图注转独立块（heading 标 `[表格]`/`[图注]`，表格截断 8000 字符）；文档级「删旧建新」（`doc_chunk_ids` 定点取旧块 id），Milvus upsert + BM25 同步；含一致性恢复）→ **检索**（`Retriever` 混合：query 侧加指令前缀（`_QUERY_INSTRUCTION`，Qwen3 官方 Instruct 格式）编码；BM25 top-30 + 向量 top-30（可按 source=note/pdf 过滤）→ RRF 融合 → 取 max(2×top_k, `rag.retriever.rerank_candidates`（默认来自 `RERANK_CANDIDATES=24`）) 个候选 → `CloudReranker` 重排 → 有序 Chunks；零全表扫描：向量路元数据随结果带回、BM25 路定点 `fetch_by_ids` 补齐）。评测（代码与产物都在 scripts/rag/ 下，gitignored；每个实验目录自成 `goldens/`（题集）+ `results/`（存档））：总方案 `scripts/rag/实验方案.md`；检索侧 `scripts/rag/retrieval_eval/run_eval.py`（黄金集 `goldens/rag_golden.jsonl`，指标 hit_rate@3/5/10 / MRR，纯脚本无 LLM judge；`strict_hit_rate@10` 已于 2026-10-05 移除——它分母含无 heading 的题（满分上限 12/43）且只看该文档排名第一的块，章节级评测改由 `scripts/rag/chunking_eval/` 的区间级指标承担）；回答侧 `scripts/rag/answer_eval/run_answer_eval.py` + `answer_evaluation.py`（忠实度/答题相关性 LLM-judge + `check_citations` 引用校验，`aggregate` 聚合、失败题不进分母）；切块侧 `scripts/rag/chunking_eval/`（Chroma 式 span recall/precision/IoU + 内在体检）；参数标定 `scripts/rag/calibration/`。

存储与模型：
- `VectorStore` — Milvus（`pymilvus.MilvusClient`，单 collection `config.rag.storage.collection`="paperflow"）；`config.rag.storage.uri` 默认 `http://localhost:19530` 连 Standalone（`docker compose up -d` 起 etcd+minio+milvus，gRPC 19530 / 健康检查 9091，数据落 `data/infra/milvus/`）；传本地文件路径则走 Milvus Lite 内嵌（单测用，无需常驻服务）
- `Bm25Index` — rank_bm25 + jieba；是向量库文本的**投影**，启动时从 `store.all_documents()` 重建
- `CloudEmbedder`（`core/llm/embedding.py`）— 云端 `Qwen/Qwen3-Embedding-0.6B`（OpenAI 兼容 `/v1/embeddings`，默认硅基流动；1024 维，客户端 L2 归一化，维度走静态映射不发网络）；`CloudReranker`（`core/llm/rerank.py`）— 云端 `Qwen/Qwen3-Reranker-0.6B`（`/v1/rerank`，返回降序下标）。协议 `Embedder`/`Reranker` 与实现同文件同层（spec 2026-10-05-embedding-cloud-startup）
- 端点/模型经 `config.rag.embedding`（RAG 用）与 `config.intent.encoder`（意图路由独立实例）配置；本地 sentence-transformers 栈已退役（无 `resolve_model_dir`、无本地权重下载），api_key 缺失时路由退纯 BM25、检索跳过稠密路、索引明确报错

消费方：`RagRetrieveTool`（`tools/rag/`，`rag_retrieve`：参数 query / top_k / source（enum 限定 note=笔记 / pdf=论文，缺省两处都搜），每条命中展示来源、路径与正文摘录前 400 字）装配进 qa-agent 与 researcher（researcher 用它按课题盘点语料）；`ReadPdfTool` 用 `parse_pdf_cached`；`write_file`/`edit_file`/`fetch_pdf` 写盘后自动触发 `index_document`。RAG 的 `CloudEmbedder` 由 `RAGService` 内部按 `config.rag.embedding` 惰性构造，意图路由的实例由 `cli.py` 按 `config.intent.encoder` 构造——两实例互不共享（记忆检索为纯 SQL LIKE，不用向量）。

### Citations

`paperflow/citations/` — 引用管理（溯源落地）。`references.bib` 是引用库**真相源**：追加 + 按条目原文块删除，两种原语都不重写其余内容（用户手工维护的分节注释与未触碰条目逐字节保留）。`bib.py` 轻量读写条目（查找/去重/追加/按 key 删除）；`corpus.py` 是「语料里有哪些论文」的易变投影（note H1 + PDF 解析标题 → 全标题精确匹配，按 (path, mtime_ns) 增量重建）；`manager.py` 编排：引用解析（干净全标题/路径 → key+status）、入库（语料内 PDF / 库外 EXTERNAL）、去重、渲染（author-year/numbered/bibtex/gbt7714）、调和（渲染视图回填空字段，bib 文件不动）。懒加载单例 `get_citation_manager()`，重组件（corpus 索引、TitleExtractor）首次使用才构造。

6 个引用工具（`tools/citations/`）：lookup/add/format/list 四件装配给 **noter** 与 **researcher**（researcher 自产自写：survey/gaps/idea 卡/研究计划的溯源标注与参考文献渲染）；**reviewer** 装配 `list_citations`+`lookup_citation` 做溯源核验（核验 `[来源:key§节]` 的 key 真实存在于 references.bib，不信任标注本身）；`sync_citations`/`remove_citation` 是文献库写入口，只装配 **librarian**（引用库维护 agent），**librarian** 独装全量 6 件 + `read_pdf`（仅读首页补元数据）+ ask_user。

产物溯源标注：
- **笔记**头部写 `**论文引用**: [key]`（落盘前经 `lookup_citation` 确认 key 真实性），各节关键论断标节级 `[来源:§X]`，供 reviewer 沿链回溯核对原文

### Tools

`paperflow/tools/` — 原子工具，一工具一文件，按域分包；`paperflow/tools/__init__.py` 再导出全部 12 个工具供消费方统一导入（导出符号名稳定，内部路径随便拆）：

另有**动态 MCP 工具**（不计入 12 的原子工具清单）：config.yaml 顶层 `mcp_servers` 声明的 server，其工具经 `paperflow/core/mcp/` 桥接为原生 Tool 注入 agent（命名 `mcp__<server>__<tool>`，与 skill 工具同一 `merge_tools` 装配缝），**写类工具可见但需逐次用户确认**（按 readOnlyHint 分类，缺注解按「可能写」处理），config 的 `write_tools` 预批准豁免；`readOnlyHint=true` 只读工具自动放行。配置示例见 `docs/learning/11-MCP客户端.md`（docs/ 为本地文档，不入库）。

- `file/` — 读/写/编辑/glob/grep/read_pdf/format_check（+ `atomic.py` 原子写盘）
- `search/` — `fetch_pdf`（下载：SSRF 校验 + 写盘后索引热更新；url 取检索结果（含 MCP 工具结果）中的 PDF 链接）；`_common.py` 有 `SearchRunState` 公共运行状态（`wants_run_state` opt-in：failed_urls 负缓存 + downloaded 成功短路）。检索收敛到 MCP（paper-search-mcp），直连 web_search/clients 已退役（2026-10-02，docs/adr/0012-mcp-client.md）
- `review/` — `submit_review` / `submit_download_review`（reviewer 的裁决工具）
- `rank/` — `lookup_venue_rank`（期刊/会议等级查询）
- `citations/` — 6 引用工具（`lookup_citation`/`add_citation`/`format_citations`/`list_citations` + `sync_citations`/`remove_citation`；noter/researcher 装前四件，reviewer 装 list+lookup 溯源核验，librarian 装全量六件 + `read_pdf` 补元数据）
- `rag/` — `rag_retrieve`（`RagRetrieveTool`：惰性取 RAGService 单例 + 持锁检索 + 格式化结果；装配 qa-agent 与 researcher）
- `vision/` — `analyze_figures`（`needs_parent=True`：视觉 LLM 调用归属父 agent 轮次进审计）。图提取走 pdffigures2 管线（proposal 候选 + 打分选优 + no-overlap 互斥），随后视觉模型结构化看图分析 + 嵌入落盘；key 缺失/无图/失败全降级
- `memory/` — 11 个记忆工具（`get_memory_tools()` 惰性单例 + `set_memory_context`/`get_memory_context` 运行时上下文；blocks/recall/paper_lists 三组；装配 supervisor，子 agent 各装子集）
- `orchestration/` — `spawn_sub_agent` / `ask_user_question` / `SubAgentMode`（见下）
- `common/` — `make_tools(config, tool_items, default_write_root=None)` 装配工厂：按 `root_hints` 生成 `[目录] {root}={path}` 提示（scratch 根对 LLM 不透明）、`default_write_root` 盖章到 write_file（noter→note、researcher→research）、注入 `_config`；`_http.py` 共享 HTTP 基础设施

根映射（`_root_map`）：note→`note_dir`、pdf→`pdf_dir`、research→`research_dir` 或 `workspace/research`、memory→`workspace/memory`、templates→`workspace/templates`、scratch→`workspace/tmp`。

### Orchestration

`paperflow/tools/orchestration/spawn.py` — **SpawnSubAgentTool**（`spawn_sub_agent`，`needs_parent=True`）。`aexecute(agent_type, task, mode=None, intent=None)` 先过 `_admit` 的**八道闸**（按判定顺序），全过才构造并运行子 agent：

1. **未知 agent 类型** — 不在 `list_agents()` 内 → denied（附可选清单）
2. **mode 校验** — 非 `SubAgentMode` 合法值 → denied（schema enum 已约束 LLM 生成层，此处兜底漏网）
3. **意图派发门禁** — 显式声明的 `intent` 优先按声明校验，未声明看本轮会话意图；`dispatch_allowed=False`（chitchat/out_of_scope/help/feedback/set_research_topic 等，含声明的不可派发意图）→ denied。**意图只作信号，不决定派发顺序**
4. **spawn 权限** — `_check_spawn_allowed`：supervisor 硬编码放行；其余 agent 查自己的 `AgentConfig.allowed_spawns` 白名单
5. **同会话同指纹去重**（指纹 = sha256(规范化任务文本 + mode)）— **无路径任务** 运行中去重 + 完成结果 300s 内可复用；**含路径任务** 只做运行中去重（文件可能中途变化，完成不缓存）
6. **审稿预算** — 同一父实例内 note_review/download_review/plan_review 各自 ≤3 次（计数键 `(父实例 id, mode)`），超限 denied（轮数预算下沉代码，LLM 不数轮次）
7. **每轮派发上限** — supervisor 每次 ReAct 迭代内自身派发 ≤8 路，超限 denied（下一轮重新起算）
8. **同路径在途互斥** — 任务文本抽出的目标绝对路径若正被**同一父实例**的在途派发占用 → denied（防并发静默覆盖；只拦同时在途，不拦按序重写已完成 spawn 写过的文件）

闸门状态（去重注册表、失败计数、派发账本、审稿与每轮预算计数、在途路径、产物账本）统一由 `core/agent/state.py` 的 session/run 两个状态容器持有（见 Agent and ReAct loop）。**顺序与并行由 supervisor 自主决定**，框架不做限制；每条被拒/去重/完成的派发尝试记入 supervisor 的**派发账本**，收尾核对时把「识别到的意图 + 实际派发记录 + 新落盘产物」摆给模型自查（代码只摆账本、不下结论）。

**子 agent 构造与执行**：继承父的 security_middleware / session_id / confirm_callback / ask_user_callback（子 agent 能中途问用户）；**不传**意图管线/会话（子任务是结构化任务非用户意图）；`mode` 经「当前模式：{mode}」注入 system prompt。**预算执行**：超时 = 基座超时（`config.agents.timeouts`，audit 数据校准:noter 900s/searcher 420s/reviewer 300s/researcher 1800s/qa-agent 180s,2026-09-05）+ 累计用户等待（`_UserWaitClock` 同时排除 confirm 确认与 ask_user 提问的人工等待）；`asyncio.TimeoutError`→timeout、`PermissionError`→denied、其他异常→failed；同一会话内同 agent_type 连续 2 次非 success → 结果文本追加强指令「勿再派发，改用 ask_user 请示」。**摘要提取**：末尾 2000 字符经 `StructuredOutput` 抽结构化 `digest`（按 agent_type 选 `SearcherDigest`/`ReviewerDigest`/`NoterDigest`/`ResearcherDigest`/`LibrarianDigest`/`QaAgentDigest`，未注册落 `GenericDigest`），失败回退全文摘要。

返回 `ToolResult(text=SubAgentResult.model_dump_json(), summary=model_dump())`。`SubAgentResult.status` ∈ {success, failed, timeout, denied}，`needs_attention=True` 表示「被拒且需用户介入」。只有 supervisor（和需要 reviewer/searcher 的 searcher/noter/researcher）装配此工具——权限最小化：叶子 agent 不递归。

**AskUserQuestionTool**（`ask_user_question`，`needs_parent=True`）：读 `parent.ask_user_callback`（CLI 注入，worker 线程读 stdin）；回调为 None 时 fail-safe 返回「无法交互，请基于已有信息决定」，绝不挂起。可选 `intent_options` 参数（意图确认协议）：展示编号选项、回复经 confirm 原语解析后代码级更新父 agent 会话意图。装配权限在装配层（supervisor/searcher/noter/qa-agent/researcher 有，reviewer 无）。

### Terminal

`paperflow/terminal/` — 终端交互隔离层，测试可注入。

- `io.py`：`InputIO` 契约（`read`/`confirm`/`ask`）。`PromptToolkitIO`（TTY，multiline + 历史；confirm 仅 y/n 键入，Enter 默认 No）vs `FallbackIO`（非 TTY，内置 `input()`）。`make_input_io(config)` 按 `stdin.isatty()` 二选一。`_confirm_lock` 串行化并行子 agent 的并发 confirm/ask（prompt_toolkit 会话非线程安全）
- `render.py`：`StreamRenderer`（线程安全）经 `on_event` 消费 `StreamEvent`（content/tool）。TTY = `RichBlock`（rich Live + spinner，0.08s 节流重绘）；非 TTY = `PlainBlock`（增量追加）。工具行 `[{agent_type}] Calling ...`，写/编辑工具完成后发 File written/edited 完成行；`should_print` 去重已流式展示的 root 内容；`suspend()` 在确认框/输入框前停 live（rich Live 与 prompt_toolkit 并发互相干扰——实测坑）
- `diff.py`：`compute_diff`（unified diff ±3）+ `truncate_diff`（≤200 行）——写/编辑确认前渲染 diff 预览
- 启动横幅：Codex 风格方框（`>_ paperFlow Academic Assistant` + model/workspace + Tip），无 emoji/版本/标语

### Config

`PaperFlowConfig.from_env()` 按优先级加载：环境变量（`PAPERFLOW_*`）> `config.yaml` > dataclass 默认值（DeepSeek 端点、`deepseek-v4-flash` 模型）。加载器是 **dataclass 树 + 通用递归合并** `_merge`（沿 `fields()` 下行、任意深度，未知键运行期忽略）+ **env 按路径派生**（`PAPERFLOW_` + 配置路径大写、`_` 连接：`rag.storage.uri` → `PAPERFLOW_RAG_STORAGE_URI`，`intent.router.alpha` → `PAPERFLOW_INTENT_ROUTER_ALPHA`）。`config.yaml` 顶层按模块分区（与 dataclass 树同构）：

```
llm / vision                    # 保留顶层（全局共用、最高频调整）
runtime:  workspace / agents_dir / max_risk
corpus:   note_dir / pdf_dir / research_dir / citations_bib_path
intent:   encoder{base_url,api_key,model} / router{alpha,top_k}
rag:      embedding{...,batch_size,timeout,max_retries} / retriever{top_k,bm25_topk,vector_topk,rerank_candidates,rrf_k}
          query_rewrite{...,history_messages} / chunker{max_tokens,overlap_tokens}
          indexer{table_text_limit} / storage{uri,collection,batch_size}
          grobid{endpoint,timeout} / tools{excerpt_chars}
memory:   sleeptime_enable / sleeptime_agent_frequency
session:  resume_replay / resume_replay_limit
agents:   timeouts（自由 dict，YAML-only）
mcp_servers                     # 保留顶层（本身即映射）
```

| 字段路径 | 说明 |
|---|---|
| `llm` (`LLMConfig`) | base_url / api_key / model / max_tokens(393216，给足防长草稿截断) / temperature(0.0) / timeout_connect / timeout_read / max_retries / context_window(1M) |
| `vision` (`VisionLLMConfig`) | 视觉模型（多模态图表分析）：base_url / api_key / model / max_tokens / 超时；默认 DeepSeek 视觉（与文本 LLM 同一端点/key）；api_key 留空不崩启动，图表分析调用时降级不可用 |
| `runtime.workspace` | 运行时数据根（`data/`）：milvus/memory/intents/models/audit/templates 等 |
| `runtime.agents_dir` | 插件扫描目录，默认 `agents` |
| `runtime.max_risk` | 策略引擎风险阈值，默认 "medium" |
| `compaction` | `CompactionSettings`（惰性工厂避免 config→compaction→llm→config 循环导入） |
| `memory.sleeptime_enable` / `memory.sleeptime_agent_frequency` | 后台整合开关 / 每 N 条新消息检查一次（默认 50） |
| `corpus.note_dir` / `corpus.pdf_dir` / `corpus.research_dir` | 语料库数据源根（note/pdf/research，个人绝对路径，**无默认值**，须经 config.yaml/env） |
| `corpus.citations_bib_path` | references.bib 路径（引用库真相源）。默认 `workspace/citations/references.bib`，可指向任意论文项目目录；空则回退默认 |
| `rag.grobid.endpoint` / `rag.grobid.timeout` | GROBID 服务地址（默认 `http://localhost:8070`）/ 请求超时（默认 60s，来自 `GROBID_TIMEOUT`） |
| `rag.storage.uri` / `rag.storage.collection` / `rag.storage.batch_size` | Milvus 地址（默认 `http://localhost:19530`）/ 集合名（默认 `paperflow`）/ 全表分页行数（默认 1000） |
| `rag.embedding` (`EmbeddingConfig`) | RAG 云端嵌入 + 精排：base_url / api_key / embed_model（Qwen3-Embedding-0.6B）/ rerank_model（Qwen3-Reranker-0.6B）/ batch_size / timeout / max_retries；api_key 留空不崩启动，路由退纯 BM25、检索跳稠密路 |
| `rag.retriever` (`RetrieverConfig`) | 混合检索参数：top_k / bm25_topk / vector_topk / rerank_candidates / rrf_k（唯一声明点 `paperflow/config.py`） |
| `rag.query_rewrite` (`QueryRewriteConfig`) | query 改写模型三元组 base_url / api_key / model（留空逐项继承 `llm`；model 留空 = 沿用主模型）+ history_messages（默认 6） |
| `rag.chunker` (`ChunkerConfig`) | max_tokens / overlap_tokens（唯一声明点 `paperflow/config.py`；改动触发配方哈希全量重索引） |
| `rag.indexer.table_text_limit` | 表格块文本截断上限（默认 8000） |
| `rag.tools.excerpt_chars` | 工具输出单条命中正文摘录上限（默认 400） |
| `intent.encoder` (`IntentEncoderConfig`) | 意图路由独立稠密编码器：base_url / api_key / model（仅 `base_url` / `api_key` 留空时 from_env 回填 `rag.embedding` 同名项；`model` 不继承，须显式配置）；**当前标定值 `Qwen/Qwen3-Embedding-8B`**（2026-10-05 标定实验：较 0.6B 生产路径 +0.042）；换非同款模型需重标阈值 |
| `intent.router` (`RouterConfig`) | alpha（稠密分支权重，默认来自 `ROUTER_ALPHA`，现值 0.15）/ top_k（默认来自 `ROUTER_TOP_K`，现值 3） |
| `session.resume_replay` / `session.resume_replay_limit` | --resume 屏上历史回放开关 / 条数上限（0 = 整窗） |
| `agents.timeouts` | 子 agent 超时覆盖表（noter 900 / searcher 420 / reviewer 300 / researcher 1800 / qa-agent 180;audit 数据校准,见 spec 2026-09-05-agent-timeout-recalibration）；自由 dict，**仅 YAML**（不派生 env） |
| `mcp_servers` | MCP server 接入配置（顶层 dict，仅 YAML 无环境变量形态）：每 server 声明 transport(stdio/http)/command/args/url/agents/超时/工具名单；连接失败跳过不挡启动，写类工具默认禁用。可注释示例段见 `docs/learning/11-MCP客户端.md`（gitignored 本地文档） |

环境变量（按路径派生，示例非全集）：`PAPERFLOW_LLM_API_KEY` / `PAPERFLOW_LLM_BASE_URL` / `PAPERFLOW_LLM_MODEL` / `PAPERFLOW_VISION_API_KEY` / `PAPERFLOW_VISION_BASE_URL` / `PAPERFLOW_VISION_MODEL` / `PAPERFLOW_RUNTIME_WORKSPACE` / `PAPERFLOW_RUNTIME_AGENTS_DIR` / `PAPERFLOW_RUNTIME_MAX_RISK` / `PAPERFLOW_CORPUS_NOTE_DIR` / `PAPERFLOW_CORPUS_PDF_DIR` / `PAPERFLOW_CORPUS_RESEARCH_DIR` / `PAPERFLOW_CORPUS_CITATIONS_BIB_PATH` / `PAPERFLOW_INTENT_ENCODER_BASE_URL` / `PAPERFLOW_INTENT_ENCODER_API_KEY` / `PAPERFLOW_INTENT_ENCODER_MODEL` / `PAPERFLOW_INTENT_ROUTER_ALPHA` / `PAPERFLOW_INTENT_ROUTER_TOP_K` / `PAPERFLOW_RAG_EMBEDDING_API_KEY` / `PAPERFLOW_RAG_EMBEDDING_BASE_URL` / `PAPERFLOW_RAG_EMBEDDING_EMBED_MODEL` / `PAPERFLOW_RAG_EMBEDDING_RERANK_MODEL` / `PAPERFLOW_RAG_RETRIEVER_TOP_K` / `PAPERFLOW_RAG_RETRIEVER_RERANK_CANDIDATES` / `PAPERFLOW_RAG_QUERY_REWRITE_MODEL` / `PAPERFLOW_RAG_CHUNKER_MAX_TOKENS` / `PAPERFLOW_RAG_CHUNKER_OVERLAP_TOKENS` / `PAPERFLOW_RAG_INDEXER_TABLE_TEXT_LIMIT` / `PAPERFLOW_RAG_STORAGE_URI` / `PAPERFLOW_RAG_STORAGE_COLLECTION` / `PAPERFLOW_RAG_GROBID_ENDPOINT` / `PAPERFLOW_RAG_TOOLS_EXCERPT_CHARS` / `PAPERFLOW_MEMORY_SLEEPTIME_ENABLE` / `PAPERFLOW_MEMORY_SLEEPTIME_AGENT_FREQUENCY` / `PAPERFLOW_SESSION_RESUME_REPLAY` / `PAPERFLOW_SESSION_RESUME_REPLAY_LIMIT`。`agents.timeouts` 与 `mcp_servers` 是自由 dict，仅 YAML 可配。env 恒为字符串，按目标字段当前类型做 bool/int 转换。运营类 env（`PAPERFLOW_SKIP_BOOTSTRAP` / `PAPERFLOW_FILE_MODE`）与 `PaperFlowConfig` 无关，不在本表。

### Key design decisions

- **No deterministic pipeline.** Everything — routing, tool selection, task decomposition — is driven by the LLM's ReAct loop. Tools are just JSON Schema definitions fed to the model
- **`ToolResult.summary: dict`** (default empty) — 结构化摘要通道：决策结果（policy_denied/user_denied）、spawn digest、记忆工具结构化数据都经它承载
- **`risk_level` 已强制**：PolicyEngineMiddleware 按 `max_risk` 阈值拦截 + `requires_confirm` 确认（键 = (工具名, 目标路径)）；Tool 安全元数据由注册表加载时校验
- **`allowed_agents` / `allowed_spawns` 已强制**：spawn 工具运行时校验白名单 + 意图派发门禁（supervisor 硬编码放行）
- **安全是中间件洋葱**：before（可拒绝/要求确认）→ 执行 → 逆序 after；每轮 run 结束 on_finish 可改写最终回答。所有拦截降级为 ToolResult 文本，只有 `MaxTurnsExceeded` 向上抛
- **SQL 是记忆真相源，markdown 是投影**；压缩/窗口驱逐永不删 SQL 行（Recall 完整）；记忆工具按角色分发（supervisor 7 个、子 agent 按「谁干活谁记录」各装子集，权限最小化）
- **编排归 supervisor，代码只摆账本**：意图只作信号、不决定派发顺序——选型按 `<available_agents>` 的能力说明，顺序与并行由 supervisor 自主决定；多意图轮次代码只把「识别到的意图 + 派发账本 + 产物清单」摆给模型自查，不下结论、不强制队列
- **`Agent.run()` 返回 str**；子 agent 结果经 `SubAgentResult`（status/summary/digest/needs_attention）结构化回传 supervisor
- **流式零开销**：`stream_callback`/`telemetry_callback` 为 None 时全链路保持原非流式行为（mock/无 UI 调用方不受影响）
- **意图只进根 agent**：spawn 的子 agent 门控关闭；澄清只在 CLI 层跨轮处理，不暴露给 supervisor（避免 ask_user 双问）
- **契约式 prompt,两层装配**:编排决策 LLM 运行时自主(AGENT.md 只写契约+启发式);硬不变式下沉代码——诚实性协议在 BASE_PROMPT、审稿轮数预算在 spawn 闸门

## 调查规则

1. 先读本文件建立边界与架构分层，再定位目标模块源码。
2. manifest 不够时深入 `pyproject.toml`、源码、`tests/` 与调用链核实；代码事实优先于文档概括。
3. 设计背景查 `docs/adr/`（按模块分文件，正文恒为最新设计）、历史决策查 `docs/adr/修正记录.md`；排查先查 `.agents/skills/paperflow-troubleshooting` 的已知根因表。
4. 实验与标定事实查 `scripts/<module>/` 下各实验目录的 `results/` 报告；评测产物遵循 `goldens/`（题集）+ `results/`（存档）分目录约定。
5. 运行期目录与 gitignored 产物默认不是一手事实源。

## Routing

- 上级：无（本文件即根 manifest）
- 子目录 manifest（就近优先）：[`paperflow/AGENTS.md`](paperflow/AGENTS.md)（及其 `core/` `rag/` `tools/` `citations/` `vision/` `terminal/`）、[`agents/AGENTS.md`](agents/AGENTS.md)、[`.paperflow/AGENTS.md`](.paperflow/AGENTS.md)
- Agent 插件定义：[`agents/supervisor/AGENT.md`](agents/supervisor/AGENT.md) 等各 `agents/<name>/AGENT.md`
- 设计文档索引：见上文 Architecture 一节的「设计文档索引」行；ADR 正文在 [`docs/adr/`](docs/adr/)
- 排障技能：[`.agents/skills/paperflow-troubleshooting/SKILL.md`](.agents/skills/paperflow-troubleshooting/SKILL.md)
- 配置示例：[`config.example.yaml`](config.example.yaml)
