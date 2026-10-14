# paperflow/core/agent/runtime.py
"""
Agent 基类 —— ReAct（Reasoning + Acting）循环的核心实现。

Supervisor 和所有 SubAgent 使用同一个 Agent 类，差异仅在于
构造函数传入的 ``agent_type`` 不同 —— Agent 通过 AgentRegistry
按类型加载对应的 system_prompt 和 Tool 集合。

设计依据：

- **权限最小化**：Supervisor 只加载调度类工具，子 agent 只加载领域类工具，互不越界
- **ReAct 循环**：Thought → Act → Obs → ... → Finish，LLM 自主决定何时停止
  （返回无 tool_calls 的 content 时）
- **Pull 模式**：Agent 不接收外部组装的工具列表，而是通过 agent_type 从注册表拉取
  配置，保证工具权限的集中控制
- **中间件管道**：每次工具调用依次经过 security_middleware 的 before 钩子（可拒绝/
  要求确认）→ 执行工具 → 逆序 after 钩子（洋葱模型）；每轮 run 结束经过 on_finish
  钩子（可改写最终回答）

ReAct 循环流程::

    1. 构建初始 messages = [system_prompt, user_task]
    2. LLM 调用 → response
    3. 如果 response 无 tool_calls → 经 on_finish 钩子后返回 content（结束）
    4. 如果 response 有 tool_calls → 并发经中间件管道执行
      （并行 gather + 信号量上限 4 + 确认锁串行，结果按调用顺序返回）
    5. 将 tool 结果附加到 messages → 回到步骤 2
    6. 超过 max_turns → 抛出 MaxTurnsExceeded

错误处理策略：

- **_exec_tool 中的异常被内部捕获**：JSON 解析失败、未知工具名、
  工具执行异常都转为 ToolResult(text="...")，作为正常对话流的一部分
  反馈给 LLM，由 LLM 自行决定是否重试或调整参数
- **中间件的拦截不抛异常**：ConfirmRequired / SecurityBlocked 等 SecurityError
  被 _exec_tool 捕获并转为带 summary.decision 的 ToolResult
  （user_denied / security_blocked），LLM 在下一轮看到
  决策结果后可自行调整行为
- **only MaxTurnsExceeded 向上抛**：这是唯一"不可恢复"的错误 ——
  LLM 陷入了无法自主退出的循环，需要调用方介入
"""

import asyncio
import hashlib
import json
import sys
import uuid
from typing import Callable

from paperflow.core.llm import (
    LLMClient, AssistantMessage, Message, SystemMessage, ToolMessage, UserMessage,
    tool_to_openai_schema,
)
from paperflow.core.agent.services.context_window import (
    CANCELLED_TOOL_MSG, ContextWindow,
)
from paperflow.core.agent.services.tool_executor import ToolExecutor
from paperflow.core.agent.constants import AgentEventKind
from paperflow.core.agent.domain.dto.agent_event import AgentEvent
from paperflow.core.agent.registry import AgentRegistry
from paperflow.core.skills import SkillRegistry
from paperflow.core.agent.base_prompt import BASE_PROMPT
from paperflow.core.security import ConfirmRequired, SecurityMiddleware
from paperflow.core.tool import SUMMARY_TERMINAL, ToolResult
from paperflow.core.common.text import sanitize_surrogates

#: Agent 与意图识别之间只有一个缝：可选的集成适配器。未装配时整套意图层不存在。
from paperflow.core.intent.services.service import IntentService

#: 同路径写/编辑串行锁注册表（键 = 目标文件路径）。同一 message
#: 并行发两个 edit_file 改同一文件时，双方都在对方决策前弹确认（「a」授权只覆盖
#: 先到者），且并发读改写同一文件有丢写竞态——写类工具按路径
#: 加锁串行化，后到者等前者完整走完确认+执行，授权键已入集合则不再弹框。
def _compute_agent_version(config) -> str:
    """agent 配置的内容哈希（sha256 前 16 位）：system_prompt + 每个工具的全签名。

    「这个 agent 会怎么表现」由 AGENT.md 正文和它拿到的工具（名称、描述、参数
    schema、安全元数据）共同决定——用内容哈希而非 git commit：脏工作树里也精确，
    且按 agent 各自变化。审计 agent_started 事件用它回答「行为异常是不是配置刚改过」。

    Args:
        config: AgentConfig，注册表加载的完整 agent 配置

    Returns:
        str，16 位十六进制的内容哈希。
    """
    h = hashlib.sha256()
    h.update((config.system_prompt or "").encode("utf-8"))
    for t in sorted(config.tools, key=lambda t: t.name):
        signature = "\n".join([
            t.name, t.description or "",
            json.dumps(t.parameters or {}, sort_keys=True, ensure_ascii=False),
            str(t.risk_level), ",".join(t.side_effects or []),
            str(t.provenance),
            ",".join(t.root_hints or []),
        ])
        h.update(signature.encode("utf-8"))
    return h.hexdigest()[:16]


class MaxTurnsExceeded(Exception):
    """ReAct 循环在 max_turns 轮内未产生最终回答时抛出。

    这是 Agent 内置的安全阀 —— 防止 LLM 陷入无限 tool-calling 循环
    （例如 LLM 反复调用同一个工具但不用其结果给出最终回答）。
    调用方（Supervisor 或 CLI）捕获此异常后应终止任务并向用户报告。
    """


class Agent:
    """ReAct 循环的执行单元，Supervisor 和 SubAgent 共用。

    构造方式（pull 模式）::

        agent = Agent(
            llm=llm_client,
            agent_registry=registry,
            agent_type="paper-agent",
            security_middleware=[AuditMiddleware(), PolicyEngineMiddleware()],
            confirm_callback=my_confirm_handler,
        )
        result = await agent.run("搜索异构图神经网络的最新论文")

    Agent 通过 ``agent_type`` 从注册表拉取:
    - system_prompt:注入 LLM 的行为规范
    - tools:本 Agent 可调用的工具集合
    - allowed_spawns:本 Agent 能 spawn 哪些子 agent

    安全模型:
    - ``security_middleware``:每次工具调用的守卫链,before 可拦截或要求用户确认,
      after 在工具执行后(含被拦截时)以逆序运行;每轮 run 结束时 on_finish 可改写
      最终回答
    - ``confirm_callback``:确认决策回调,默认 fail-safe 拒绝
    - ``session_id``:跨多轮 run 的会话标识,未传入时自动生成
    - ``_trace_id``:每次 run 自动生成的追踪 ID,注入上下文供中间件审计

    Attributes:
        llm: LLMClient，对话客户端
        agent_registry: AgentRegistry，按 agent_type 拉取配置
        agent_type: str，Agent 类型标识
        security_middleware: list[SecurityMiddleware]，before 顺序 / after 逆序 / on_finish 顺序执行
        confirm_callback: 确认回调（None 时用 fail-safe 的 _default_confirm，始终拒绝）
        session_id: str，会话标识（跨多轮 run 一致）
        memory / block_manager / message_manager / agent_manager / compaction / structured: 记忆与结构化输出服务句柄（None 时相关路径零开销跳过）
        intent_service: IntentService | None，意图识别集成适配器（None = 意图层不存在；仅 CLI 构造的 supervisor 装配）
        last_intent: IntentOutput | None，本轮意图（只读委托给 intent_service，供跨轮回写读取）
        max_turns: int，ReAct 循环轮次上限（超过抛 MaxTurnsExceeded）
        stream_callback: 回调 | None，流式事件回调（None = 非流式路径）
        skill_registry: SkillRegistry | None，skill 体系（L1 清单注入与工具并入）
        tools: dict[str, Tool]，本 Agent 可调用的工具表
        window: ContextWindow，in-context 窗口（消息缓冲 + 持久化 id 追踪，一致性由它保证）
        _trace_id / _inherited_trace_id: str | None，本次 run 的追踪 ID / 构造时继承的父追踪 ID
        _instance_id: str，实例唯一标识（按「父实例」键控的预算计数用，与 trace_id 区分）
        _current_turn: int，当前 ReAct 轮次（spawn 摘要提取借此归属父轮次）
        _tool_schemas: list[dict]，预计算的 function calling JSON Schema
        _has_human_confirm: bool，是否有真实人工确认回调（区分 auto_denied 与 user_denied）
    """

    def __init__(
        self,
        llm: LLMClient,
        agent_registry: AgentRegistry,
        agent_type: str,
        security_middleware: list[SecurityMiddleware] | None = None,
        confirm_callback: Callable[[ConfirmRequired], bool] | None = None,
        intent_service: "IntentService | None" = None,
        session_id: str | None = None,
        memory=None,                # Memory | None
        agent_manager=None,         # AgentManager | None
        block_manager=None,         # BlockManager | None
        message_manager=None,       # MessageManager | None
        compaction=None,            # CompactionSettings | None
        structured=None,            # StructuredOutput | None
        max_turns: int = 20,
        stream_callback: Callable[[AgentEvent], None] | None = None,
        skill_registry: SkillRegistry | None = None,   # Skill 注册表；None = 无 skill 体系
        trace_id: str | None = None,   # 继承的追踪 ID；None = 每次 run 自行生成
    ):
        """
        Args:
            llm: LLM 客户端实例
            agent_registry: Agent 注册表，从中按 agent_type 拉取配置
            agent_type: Agent 类型标识符（对应 agents/<agent_type>/ 目录）
            security_middleware: 安全中间件列表，按顺序执行 before / 逆序执行 after；每轮 run 结束时顺序执行 on_finish
            confirm_callback: async 确认回调，接收 ConfirmRequired，返回 bool；None 时使用 fail-safe 的 _default_confirm（始终拒绝）
            intent_service: 意图识别集成适配器（可选预处理层）。提供时 ReAct 循环在
                开头调 begin、收尾调 finish；None 时整套意图层不存在
                （纯 ReAct，无 INTENT 块、无澄清、无追问继承）。
            session_id: 会话标识,跨多次 run 保持一致,便于审计聚合;None 时 自动生成 8 位 hex
            memory: Memory 实例(可选),compile() 输出 system 记忆块注入 head (每轮重建);None 时跳过
            agent_manager: AgentManager 实例(可选),当前仅持有供上层(CLI)取用
            block_manager: BlockManager 实例(可选),记忆块 CRUD 的服务句柄 (记忆工具经它读写核心记忆)
            message_manager: MessageManager 实例(可选),对话落盘(Recall) + in-context 跨轮回放;None 时记忆相关路径零开销跳过
            compaction: CompactionSettings 实例(可选),触发时只压缩 in-context 窗口(驱逐旧对话 + 插摘要),不删 SQL 原始消息
            structured: StructuredOutput 实例(可选),compaction 摘要生成路径 消费;None 时压缩不触发(降级,CLI 接线后恢复)
            max_turns: ReAct 循环最大轮数,防止死循环
            stream_callback: 流式事件回调(CLI 渲染器消费);None = 非流式路径 ——run() 保持调 chat(),mock/无 UI 调用方零影响
            skill_registry: Skill 注册表(可选)。提供时按 agent_type 计算 L1 <available_skills> 清单块注入 head(静态,每轮重建 head 时原样携带);None 时整块省略零开销
            trace_id: 继承的追踪 ID(可选)。spawn 工具传父 agent 的当前 trace_id,使子 agent 与父共享同一次用户任务的去重池与审计链;None 时每次 run 自行生成新 trace_id(默认,行为不变)

        """
        # Pull 模式:从唯一注册表按类型加载完整配置
        config = agent_registry.get_config(agent_type)
        #: agent 配置内容哈希（审计 agent_started 用）：AGENT.md 正文 + 工具全签名
        self._agent_version = _compute_agent_version(config)

        #: Agent 注册表(构造子 agent 时需要)
        self.agent_registry = agent_registry

        #: LLM 客户端（async 接口）
        self.llm = llm

        #: Tool 字典，key = tool.name，供 _exec_tool 快速查找
        self.tools = {t.name: t for t in config.tools}

        #: 注入 LLM 的系统提示词 = 角色定义(AGENT.md 正文) + 行为基座(BASE_PROMPT,
        #: 全 agent 共有)。拼接在同一消息内——head 结构、记忆刷新插入位不动;
        #: 基座落在末尾贴近对话,利于规则遵循
        self.system_prompt = config.system_prompt + BASE_PROMPT

        #: Skill 注册表（spawn 构造子 agent 时透传用）
        self.skill_registry = skill_registry

        #: L1 <available_skills> 清单块（静态；空串 = 无已注册 skill，head 整块省略）
        self.skills_block = skill_registry.skills_block() if skill_registry else ""

        #: <available_agents> 清单块（本 agent 可派发目标，空串 = 无可派发目标，整块省略）
        self.agents_block = agent_registry.agents_block(agent_type)

        #: Agent 类型标识符
        self.agent_type = agent_type

        #: ReAct 循环最大轮数安全阀
        self.max_turns = max_turns

        #: 流式事件回调（CLI 渲染器）；None = 非流式路径（mock 测试/无 UI 调用方）
        self.stream_callback = stream_callback

        #: 预计算的 OpenAI function calling JSON Schema 列表
        #: 在构造时转换一次，避免每轮 run 都重复转换
        self._tool_schemas = [tool_to_openai_schema(t) for t in config.tools]

        #: 安全中间件管道,空列表时执行器退化为直通行为(不经过任何守卫)
        self.security_middleware = security_middleware or []

        #: 用户确认回调；未提供时使用 fail-safe 的 _default_confirm
        self.confirm_callback = confirm_callback or self._default_confirm

        #: 是否有真实人工确认回调（用于区分 auto_denied 与 user_denied）。
        #: 用构造时标志而非回调身份判断：spawn 会包装 confirm_callback
        #: （_wrap_confirm_callback），包装后身份失效，构造标志不受影响。
        self._has_human_confirm = (
            confirm_callback is not None
            and confirm_callback is not self._default_confirm
        )

        #: 会话标识：跨多轮 run 保持一致，供中间件审计日志聚合
        self.session_id = session_id or uuid.uuid4().hex[:8]

        #: 核心记忆挂载（Memory | None）：compile() 输出 <memory_blocks> 注入 head
        self.memory = memory
        self.agent_manager = agent_manager
        self.block_manager = block_manager
        self.message_manager = message_manager
        #: 压缩设置（CompactionSettings | None）：触发时只改 in-context 窗口
        self.compaction = compaction
        #: 结构化输出（StructuredOutput | None）：compaction 摘要生成路径消费
        self.structured = structured

        #: token 估算的校准系数：本地估算与实际用量的比值，每次拿到真实 prompt_tokens
        #: 就更新一次（见 _calibrate_token_ratio）。1.0 = 未校准。
        self._token_ratio = 1.0
        #: 本轮发送前的本地估算，供返回后用真实用量校准（0 表示本轮还没算过）。
        self._last_sent_estimate = 0

        #: in-context 窗口：本会话要发给模型的那批消息 + 它们在 messages 表里的 id。
        #: run() 每轮经它从 MessageManager 重新加载（跨轮消息经 SQL 回放，不在内存里
        #: 跨 run 累积）；消息与 id 的一致性由它内部保证，见 ContextWindow。
        self.window = ContextWindow(message_manager=message_manager,
                                    agent_manager=agent_manager,
                                    session_id=self.session_id)

        #: 当前 run 的追踪 ID，每次 run 开始时重新生成，注入 ToolContext
        self._trace_id: str | None = None

        #: 构造时继承的追踪 ID（spawn 场景由父 agent 传入）。非 None 时 run()
        #: 不再生成新 trace_id，而是沿用此值——去重池按 trace_id 键控，子 agent
        #: 由此与父共享同一次用户任务的去重池。
        self._inherited_trace_id: str | None = trace_id

        #: 当前 ReAct 轮次:run() 每轮循环开头更新。spawn 摘要提取的 LLM 调用
        #: 读父 agent 的此属性归属轮次(父在做摘要提取,归父的 trace/轮次)。
        self._current_turn: int = 0

        # 意图识别（可选预处理层）：只持一个适配器引用，None 即「关」——ReAct
        # 循环对意图的内部一无所知，几个钩子点全走它。
        self.intent_service = intent_service
        #: 实例唯一标识：跨 run 稳定，供按「父实例」键控的预算计数使用（如审稿预算），
        #: 与按 run 生成的 _trace_id 区分——子 agent 继承父 trace_id，用 trace_id 键控
        #: 会把同一轮里多个同类父实例的预算混在一起。构造即固定，不再变化。
        self._instance_id: str = uuid.uuid4().hex

        #: 工具执行管道（中间件洋葱 + 三把锁 + 参数解析 + tool_end 收口）。
        #: 只注入构造后不变的依赖；tools / confirm_callback / 流式回调在调用期现取
        #: ——它们会被替换（spawn 包装确认回调、REPL 之后挂渲染器、测试换工具表）。
        self.tool_executor = ToolExecutor(
            middleware=self.security_middleware,
            agent_type=self.agent_type,
            session_id=self.session_id,
            instance_id=self._instance_id,
            has_human_confirm=self._has_human_confirm,
        )

        # opt-in 注入：仅对声明 needs_parent 的工具注入父引用。
        # 原子工具不需要 parent；只有嵌套子 agent 的工具声明——权限最小化。
        # 必须放在所有 __init__ 属性赋值之后：attach_agent 可能被工具覆写为
        # 读取父 Agent 属性（如 session_id）的访问器，提前注入则构造期父引用
        # 不完整——被攻陷工具此时读到的 session_id 等仍是缺省值（安全前瞻坑位）。
        # needs_skill_registry 是同类 opt-in：要读 skill 资源却走不了 load_skill 的
        # 确定性工具（如 format_check）靠它拿到注册表，把资源解析成绝对路径。
        for t in self.tools.values():
            if getattr(t, "needs_parent", False):
                t.attach_agent(self)
            if getattr(t, "needs_skill_registry", False) and self.skill_registry is not None:
                t.attach_skill_registry(self.skill_registry)

    async def _default_confirm(self, cr: ConfirmRequired) -> bool:
        """默认 fail-safe：无人值守时拒绝。

        Args:
            cr: ConfirmRequired，待确认的工具调用

        Returns:
            恒 False（无人值守 fail-safe 拒绝）。
        """
        return False

    #: messages 只读 property（OpenAI wire 格式视图）。
    #: 只读：外部（CLI/测试）可观察但不可改，写入统一走 self.window。
    @property
    def messages(self) -> list[dict]:
        """in-context 消息的只读 wire 视图（外部可观察不可改）。

        Returns:
            OpenAI wire 格式的消息 dict 列表。
        """
        return self.window.wire()

    def _index_text(self) -> str | None:
        """读取 memory_filesystem.md（MemFS 自动生成的文件树索引）内容。

        索引是渐进暴露的关键：非 system 块不进 compile，但索引让 LLM 知道「有哪些
        文件可读」。无 MemFS 装配（block_manager 无 memfs）时返回 None。
        """
        bm = self.block_manager
        # GitEnabledBlockManager（子类，block_manager.py:249）：构造时才挂上 self.memfs = MemFS(...)
        memfs = getattr(bm, "memfs", None)
        if memfs is None:
            return None
        path = memfs.memory_dir / "memory_filesystem.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def _memory_message(self) -> Message | None:
        """编译当前记忆为 system 消息：核心块 + 文件系统索引。

        有 block_manager 时先从 BlockManager 重建 memory（Memory(blocks=...）——
        记忆工具（memory_replace 等）在会话内编辑块后，下一轮 LLM 调用即见新内容
        （块变更即时生效，而非重启才可见）。
        """
        if self.memory is None:
            return None

        # BlockManager，负责读写 SQLite 数据库里 blocks 表的管理器。list_blocks() 就是去查库，把当前所有块读出来，返回 list[Block]（每个 Block 是 pydantic 模型，含 label/value/版本号等）
        # 每轮都从 BlockManager 重新读 blocks 重建 memory 对象——这就是「记忆会话内即时生效」的机制
        # 注意：这里构建的是记忆块（blocks 表）——不是历史会话。历史会话在 messages 表，由 MessageManager 管
        if self.block_manager is not None:
            from paperflow.core.memory.schemas.memory import Memory
            # 把「缓存的记忆对象」扔掉，从数据库重新读出所有块，拼一个新的 Memory 实例
            self.memory = Memory(blocks=self.block_manager.list_blocks())

        # compile() 只渲染 assistant/profile 两块 + 文件树索引（渐进暴露，其余块按需读）。compiled 的实际内容长这样：
        #   <memory_blocks>
        #   <block name="assistant">…助手工作方式记忆…</block>     ← 只有 assistant/profile 两块的内容
        #   <block name="profile">…用户画像…</block>
        #   </memory_blocks>
        #   <memory_filesystem>
        #   …memory_filesystem.md 的内容…                    ← 文件树索引（文件名/结构，不是正文）
        #   </memory_filesystem>
        compiled = self.memory.compile(index_text=self._index_text())
        if not compiled:
            return None
        return SystemMessage(content=compiled)

    def _intent_history(self) -> list[tuple[str, str]]:
        """截取最近若干轮对话文本，供意图层判定时参考上下文。

        只取 user / assistant 的**正文**：工具结果又长又噪（检索命中、文件内容），
        且 assistant 的最终答复已经概括过它们；带 tool_calls 但正文为空的 assistant
        消息也不进（那是工具调用的载体，不是「它说过的话」）。**assistant 侧必须保留**
        ——「再下载一篇」「那 Figure 3 呢」的指代目标就是它上一轮说的话。按时间正序
        返回，末尾是最近一轮。

        窗口大小取自意图层（`history_messages`）；意图层未装配时不会被调用。

        Returns:
            list[tuple[str, str]]：[(role, content)]，role 为 "user" / "assistant"；
            正序、已截到窗口上限。
        """
        limit = getattr(self.intent_service, "history_messages", 0)
        if not limit or not self.window.messages:
            return []
        history = [(m.role, m.content.strip())
                   for m in self.window.messages
                   if m.role in ("user", "assistant") and (m.content or "").strip()]
        return history[-limit:]

    async def _build_head(self, task: str) -> list[Message]:
        """构建本轮 ReAct 循环的头部消息列表（system 层 + 用户任务）。

        **调用前 in-context 历史必须已加载**（`run()` 里先 `window.load()` 再建 head）——
        意图层要读最近几轮对话文本才能读懂「再下载一篇」这类指代。

        此方法在每次 run 开始时被调用一次（不在 ReAct 轮次内重复），用于组装 LLM 输入的前置部分（system 消息）。
        它按顺序拼接六块内容：
            1. system: AGENT.md 系统提示（来自 agent 配置，定义角色与行为规范）
            2. system: SKILLS 清单块（L1 渐进披露清单，若装配了 SkillRegistry 且有可见 skill）
            3. system: 可派发子 agent 清单块（本 agent 有权派发的目标，按 allowed_spawns 渲染）
            4. system: 记忆块（Memory.compile() 输出的 assistant/profile + 文件树索引，若有）
            5. system: 意图规则块与 INTENT 块（仅装配意图服务时；规则块给出字段语义与
               非派发意图的处理说明，INTENT 块是路由先验）
            6. 末尾追加 user task。

        意图层的澄清：装配意图服务时由 begin 内部**同步**调 ask 回调（不经 supervisor
        的 LLM 转手——「要问」由代码强制、不靠提示词自觉），答案在代码层落地为意图后
        ReAct 直接以正确意图启动，无跨轮挂起。

        Args:
            task: 本轮用户输入文本（原始任务）。

        Returns:
            list[Message]: 头部消息列表 [system_prompt, skills(可选), available_agents(可选), memory(可选), rules(可选), intent(可选), user_task]。
        """
        # ====== 第1层：AGENT.md 系统提示 ======
        head: list[Message] = [SystemMessage(content=self.system_prompt)]

        # ====== 第2层：SKILLS 清单（L1 渐进披露，静态） ======
        # skill 指令的约束力声明写在块内；无已注册 skill 时 skills_block 为空串，整块省略
        if self.skills_block:
            head.append(SystemMessage(content=self.skills_block))

        # ====== 第 2.5 层：可派发子 agent 清单（本 agent 的派发权限视图，静态） ======
        # 派发顺序与并行由派发方自主决定，因此它必须先知道有哪些子 agent、各自能做什么；
        # 清单是自身 allowed_spawns 的派生视图（supervisor 为全部），无可派目标时为空串，
        # 整块省略（不产生空 system 消息）。
        if self.agents_block:
            head.append(SystemMessage(content=self.agents_block))

        # ====== 第3层：记忆块（核心记忆 + 文件系统索引） ======
        if self.memory is not None:
            m = self._memory_message()
            if m is not None:
                head.append(m)

        # ====== 第4层：意图识别（可选预处理层）======
        # 未装配意图服务时整块零开销跳过。装配时：先注入规则块（字段语义与
        # 非派发意图的处理说明，由意图层产出而非写死在 AGENT.md），再跑前置
        # 预处理拿 INTENT 块与可能被澄清答案附录过的任务文本。
        if self.intent_service is not None:
            if self.intent_service.rules_block:
                head.append(SystemMessage(content=self.intent_service.rules_block))
            turn = await self.intent_service.begin(task, history=self._intent_history())
            task = turn.task
            if turn.head_block:
                head.append(SystemMessage(content=turn.head_block))

        # ====== 第5层：用户任务 ======
        # 最后将当前用户输入作为 user 消息追加（含澄清答案附录，见 IntentService.begin）。
        head.append(UserMessage(content=task))
        return head

    def _refresh_head_memory(self, head: list[Message]) -> None:
        """会话内刷新 head 里的记忆 system 消息（memory 工具编辑后即时生效）。

        有 block_manager 时在 ReAct 每轮开头重建记忆块并替换 head 中旧的
        <memory_blocks> 消息——同一轮里 memory_replace 改的块,下一轮 LLM 调用即见。
        无记忆装配（memory 或 block_manager 为 None）时零开销空操作。

        Args:
            head: list[Message]，本轮 head 消息列表（就地替换/插入记忆消息）
        """
        if self.memory is None or self.block_manager is None:
            return
        new_msg = self._memory_message()
        for i, m in enumerate(head):
            if m.role == "system" and (m.content or "").startswith("<memory_blocks>"):
                if new_msg is None:
                    head.pop(i)
                else:
                    head[i] = new_msg
                return
        if new_msg is not None:
            head.insert(1, new_msg)

    def _needs_compaction(self, messages: list[Message]) -> bool:
        """in-context 消息是否超压缩阈值（只检查，执行在 run() 里）。

        依赖 message_manager 存在:无持久化层时压缩无从安置(摘要无处回放),跳过。

        这里顺手记下本轮发送前的本地估算——LLM 返回后用它和真实 prompt_tokens 算
        校准系数。**无论是否装配压缩都要记**，否则校准会拿上一轮的旧估算对这一次的
        用量，比例算出来是错的。

        Args:
            messages: list[Message]，当前 in-context 消息

        Returns:
            True 表示已超压缩阈值（无持久化层时恒 False）。
        """
        from paperflow.core.memory.services.compaction import estimate_tokens, should_compress
        self._last_sent_estimate = estimate_tokens(messages)
        if self.compaction is None or self.message_manager is None:
            return False
        return should_compress(messages, self.compaction, self.llm.context_window,
                               estimate=self._last_sent_estimate, ratio=self._token_ratio)

    def _calibrate_token_ratio(self, response) -> None:
        """用一次真实的 prompt_tokens 校准本地估算系数（同步，无返回值）。

        只在端点报了用量时生效：流式未开 include_usage、或兼容端点不报 usage 时，
        系数保持不变（沿用本地估算原值）。假回复（鸭子类型、没有 meta）同样跳过。

        Args:
            response: 本轮 LLM 返回的消息（可能带响应信封）。
        """
        from paperflow.core.memory.services.compaction import blend_token_ratio
        meta = getattr(response, "meta", None)
        usage = getattr(meta, "usage", None) if meta is not None else None
        actual = getattr(usage, "prompt_tokens", None) if usage is not None else None
        self._token_ratio = blend_token_ratio(
            self._token_ratio, actual, self._last_sent_estimate)

    async def run(self, task: str) -> str:
        """
        执行 ReAct 循环，返回 LLM 的最终文本回答。

        这是 Agent 的唯一公共入口。调用方（CLI、Supervisor 的 SpawnSubAgentTool）
        只需要传入任务文本，等待返回结果。

        Args:
            task: 用户任务文本（对于 Supervisor 是原始用户输入；对于 SubAgent 是 Supervisor 拆分后的子任务）

        Returns:
            LLM 的最终文本回答（经过所有中间件的 on_finish 钩子改写）

        Raises:
            MaxTurnsExceeded: 超过 max_turns 轮仍未停止


        ReAct 循环步骤::

            1. 生成本次 run 的 trace_id（32 位 hex，W3C 128-bit）并清洗 task 的未配对 surrogate
            2. 构建 head：① AGENT（AGENT.md 系统提示）→ ② SKILLS 清单块（若装配
               SkillRegistry 且有可见 skill）→ ③ 可派发子 agent 清单块（本 agent
               有权派发的目标）→ ④ Memory.compile()（system/ 记忆块，若有）→ ⑤ 意图
               规则块与 INTENT 块（装配意图服务时）→ user_task。
               意图层的澄清在本步内同步问用户并代码级落地意图
            3. 从 MessageManager 加载该会话的 in-context 消息（跨轮回放），当前
               user task 落盘；消息归属 self.window（in-context 窗口）
            4. 调用 LLM 前检查压缩（compaction.should_compress → run_compaction
               驱逐旧对话 + 插摘要，只改 in-context 窗口、不删 SQL），随后调用 LLM
            5. 如果无 tool_calls → 顺序执行各中间件的 on_finish 钩子，最终回答
               落盘后返回改写后的 content（LLM 判定任务完成）
            6. 如果有 tool_calls → 并发执行（gather，结果按调用顺序返回），将
               ToolResult 落盘并附加到 in-context 消息
            7. 回到步骤 4，LLM 根据工具执行结果继续推理
            8. 若超过 max_turns → 抛出 MaxTurnsExceeded（安全阀）
        """
        # 每次 run 独立追踪 ID：同一 conversation 的多次 run 由 trace_id 区分。
        # spawn 的子 agent 继承父 trace_id——去重池（get_run_state 按其键控）因此
        # 在「一次用户任务」内跨 agent 共享：supervisor 超时重试派发的新 paper-agent
        # 不会重复下载父任务已下载过的论文。
        self._trace_id = self._inherited_trace_id or uuid.uuid4().hex

        # 信任边界：清洗用户输入里的未配对 surrogate（外部粘贴/合成文本可能携带，
        # 见 core/security/text.py）——否则下游意图管线/实体提取在脏字符上工作，且
        # conversation.prev_user_input 会把脏字符带入下一轮。正常输入零开销（无匹配回原串）。
        task = sanitize_surrogates(task)

        # agent 起跑审计：每个 agent 实例每次 run 一条——原始任务文本（supervisor=
        # 用户原话，子 agent=子任务文本）+ 配置版本 + 模型。用户 query 的取证锚点。
        self._emit_agent_start(task)

        #: in-context 窗口每轮重建：跨轮回放统一经 MessageManager(SQL) 加载，避免缓冲
        # 跨 run 残留导致重复加载（每次都从权威源重新 load）。必须先于 head 构建——
        # 意图层要读最近几轮对话文本（它得读懂指代类输入）。
        self.window.load()

        # head:① AGENT ② SKILLS ③ 可派发子 agent 清单 ④ Memory ⑤ 意图规则块+INTENT 块,每轮重建
        # 不进累积;末尾 user task。意图层澄清在 _build_head 内(经 begin)同步问用户并落地。
        head = await self._build_head(task)
        #: 当前 user task 落盘(Recall),下轮经窗口回放；它排在 head 末尾参与每次
        # 请求,不进消息缓冲,故只落盘记账。
        self.window.persist(head[-1])

        #: 截断续写累积器：半截回答在此暂存，完整回答返回前合并。
        #: 只在截断→续写场景使用；非截断路径保持空列表，零额外行为。
        accumulated: list[str] = []

        for turn in range(self.max_turns):
            # 记录当前轮次:spawn 摘要提取的 LLM 调用据此归属父 agent 的当前轮
            self._current_turn = turn
            # 记忆块会话内即时生效:memory 工具编辑后,下一轮 LLM 调用即见新块
            # (head 是本地列表,_refresh_head_memory 就地替换记忆消息)。
            self._refresh_head_memory(head)

            #: LLM 输入 = head 前段(AGENT/SKILLS/memory/INTENT) + in-context 回放历史 +
            #: 末尾当前 user task(恒末位——否则 LLM 会把回放历史里的旧任务误当当前任务)。
            messages = list(head[:-1]) + self.window.messages + [head[-1]]

            # 压缩检查:只改 in-context 窗口(驱逐旧对话 + 插摘要),SQL 原始消息由 MessageManager 保留(Recall 可追溯)。
            # structured 未注入(摘要生成器缺失) 时压缩不触发——避免 None.extract 崩溃,CLI 接线后恢复。
            if self._needs_compaction(messages) and self.structured is not None:
                # 截断续写与压缩重建互斥:压缩可能驱逐"半截+续写提示"(in-context 重建),先弃掉累积器里的半截——续写无参照即完整重答,避免「半截 + 完整重答」重复交付。
                accumulated.clear()
                from paperflow.core.memory.services.compaction import run_compaction
                new_window = await run_compaction(
                    self.window.messages, self.compaction, self.llm, self.structured)
                # 整体替换窗口：摘要先落盘、id 列表更新为「摘要 + 保留尾部」——压缩
                # 产物跨轮持久，下轮回放摘要、不回放被驱逐旧消息（SQL 原始消息仍全量
                # 保留，Recall 完整）。「认领旧 id」与「替换」的先后关系封在 replace 内。
                self.window.replace(new_window)

                # head[:-1]：① AGENT（AGENT.md 系统提示）② SKILLS 清单块（若有可见 skill）③ Memory Blocks（核心记忆块，如 assistant/profile）④ INTENT Block（意图识别结果，若启用）
                # window.messages：从 MessageManager（SQL 持久化层）加载的该会话历史消息，加上本轮已产生的 assistant/tool 交互消息
                # head[-1]：当前的 user task 消息
                messages = list(head[:-1]) + self.window.messages + [head[-1]]

            # 流式门控：挂了 stream_callback 才走 chat_stream（否则保持 chat()）。
            # mock LLM 只有 chat 方法，无条件换 chat_stream 会让 MagicMock 不可
            # await 抛 TypeError——门控同时是零开销路径（无 UI 调用方不受影响）。
            tools = self._tool_schemas if self._tool_schemas else None

            # response 是一条 AssistantMessage（字段语义见 core/llm/domain/dto/messages/assistant_message.py）：
            # 纯工具调用时 content 被规整为空串；tool_calls 的 arguments 是 JSON 字符串，
            # 由 _exec_tool 负责 json.loads；truncated 为真时走续写而不是交付半截内容。
            if self.stream_callback is not None:
                response = await self.llm.chat_stream(
                    messages, tools=tools,
                    on_delta=lambda d: self.stream_callback(
                        AgentEvent(AgentEventKind.CONTENT, d, self.agent_type)),
                    telemetry_callback=self._make_llm_telemetry(turn),
                )
            else:
                response = await self.llm.chat(
                    messages, tools=tools,
                    telemetry_callback=self._make_llm_telemetry(turn))

            # 用真实 prompt_tokens 校准本地 token 估算（端点没报用量就保持原系数）
            self._calibrate_token_ratio(response)

            # LLM 没有请求调用任何工具（判定任务完成）：返回无 tool_calls 的纯文本消息
            if not response.tool_calls:
                if response.truncated:
                    # 截断场景：不能把残缺内容作为最终回答返回，否则会静默交付不完整信息。
                    # 采用“续写策略”：
                    # 1. 将已生成的半截内容暂存到 accumulated 列表；
                    # 2. 将 LLM 的响应（含已生成内容）追加到 in-context 缓冲区；
                    # 3. 追加一条用户角色消息，明确指示 LLM 从断点继续，避免重复输出；
                    # 4. 持久化这些消息（落盘），然后 continue 进入下一轮循环，让 LLM 续写。
                    # max_turns 安全阀确保续写次数有限，不会形成死循环。
                    accumulated.append(response.content or "")
                    self.window.append(response)
                    self.window.append(UserMessage(
                        content="上一条回答因输出长度上限被截断，请直接从断点继续输出，不要重复已输出的内容。"))
                    self.window.persist(response)
                    continue

                # 正常完成（未被截断）
                content = "".join(accumulated) + (response.content or "")
                accumulated.clear()

                # 执行安全中间件（ContentGuardMiddleware）的 on_finish 钩子（按顺序），
                # 每个中间件可以改写最终回答（如追加来源引用、注入安全声明等）
                for mw in self.security_middleware:
                    content = await mw.on_finish(self, content)

                # 最终回答(经 on_finish 改写——回放给下轮的是"用户看到的事实",
                # 内容防护追加的安全声明跨轮保留)落盘 + 进 in-context,供下轮回放。
                # 信封随之落盘:token 用量/结束原因/模型快照进 Recall,可事后按会话核算;
                # 续写场景下 content 由「半截 + 续写」拼接,信封只覆盖最后一段调用——
                # 各半截的用量已随各自的消息落盘,此处不累计。response 可能是不带信封的
                # 替身对象,故用 getattr 取。
                final = AssistantMessage(content=content,
                                         meta=getattr(response, "meta", None))
                self.window.append_and_persist(final)
                return content

            # LLM 请求调用工具：将 assistant 消息（含 tool_calls）加入 in-context 并持久化到数据库，以便后续轮次（或跨 run）能回放该条消息。
            self.window.append_and_persist(response)

            # 并发执行 LLM 请求的所有工具调用:同一 message 的多个工具调用用 gather 并行
            # 1. 使用 asyncio.gather 并行执行同一 message 中的多个工具调用，提升效率。
            # 2. 每个工具调用通过 _exec_tool 处理，内部包含中间件管道、参数解析、执行等。
            # 3. 并行上限使用信号量限制，防止一次性打爆外部 API 或数据库连接池。
            # 4. 确认回调（如高风险操作需用户确认）通过一个共享锁串行化，避免多个工具同时抢占 CLI 标准输入导致竞态。

            # supervisor 可以输出 8 个 spawn_sub_agent 调用，gather 把 8 个 _run_one 一起调度起来，
            # 但是只有 4 个子 agent 真的在同时跑，还有 4 个卡在信号量上排队
            sem = asyncio.Semaphore(4)
            confirm_lock = asyncio.Lock()

            # 内部协程：每个工具调用受信号量限制，并传入确认锁和当前轮次。
            async def _run_one(tc: dict) -> ToolResult:
                """受信号量约束执行单个工具调用并传入确认锁与当前轮次。

                Args:
                    tc: dict，一个工具调用（含 id 与 function）

                Returns:
                    该次工具调用的 ToolResult。
                """
                async with sem:
                    return await self._exec_tool(
                        tc, _confirm_lock=confirm_lock, turn=turn)

            # ============================================================
            # 并发执行所有工具调用，使用 asyncio.gather 实现。
            #
            # 1. 语法拆解：
            #    - response.tool_calls: LLM 返回的工具调用列表（每个元素是 dict），包含 id、function.name、function.arguments。例如 [{"id": "call_1", ...}, {"id": "call_2", ...}]
            #    - (_run_one(tc) for tc in response.tool_calls): 生成器表达式，为每个工具调用创建一个协程对象（_run_one 是 async 函数）
            #    - * 星号解包：将生成器产生的多个协程对象解包为位置参数，相当于 asyncio.gather(_run_one(tc1), _run_one(tc2), ...)。若不加 *，则传入的是一个生成器对象，类型不匹配。
            #    - asyncio.gather(...): 接收一组可等待对象（协程/Task/Future），并发调度它们执行，并等待全部完成后返回一个结果列表，顺序与输入顺序严格一致
            #    - await: 挂起当前协程（Agent 的 run 方法），直到 gather 管理的所有子协程执行完毕，将控制权交还给事件循环。
            #
            # 2. 并发模型：单线程异步并发，事件循环交错执行，适合 I/O 密集型。
            #    工具执行内部使用 asyncio.to_thread 真正实现线程池并行。
            #
            # 3. 并发控制：每个 _run_one 内部会先获取信号量 (Semaphore(4))，
            #    限制同时活跃的工具调用数，防止打爆外部资源。
            #
            # 4. 确认锁串行化：_run_one 将 confirm_lock 传递给 _exec_tool，
            #    确保用户确认操作串行执行，避免 CLI 输入竞态。
            #
            # 5. 异常处理：_exec_tool 捕获所有异常并转为 ToolResult，
            #    因此 gather 永远不会收到未捕获的异常，保证所有工具结果都能返回。
            #
            # 6. 顺序保证：gather 返回的 results 顺序与传入协程顺序完全一致，
            #    后续通过 zip(response.tool_calls, results) 可安全地将结果
            #    与 tool_call_id 对应，确保 LLM 下一轮推理上下文正确。
            # ============================================================
            try:
                results = await asyncio.gather(*(_run_one(tc) for tc in response.tool_calls))
            except asyncio.CancelledError:
                # 取消/中断路径的历史自愈：assistant(tool_calls) 已先落盘，被取消时
                # tool 结果消息永远等不到——缺配对消息的坏历史会让下一轮回放直接
                # 触发 OpenAI 400。这里为本 message 的每个 tool_call 合成一条
                # cancelled tool 消息（进窗口 + 落盘）后再抛出。gather 里已完成的
                # 调用其结果一并丢弃，按「本轮作废」语义统一记为 cancelled（文件
                # 等盘上副作用不回滚）。
                synth = [
                    ToolMessage(content=CANCELLED_TOOL_MSG,
                                tool_call_id=tc["id"])
                    for tc in response.tool_calls
                ]
                self.window.append_and_persist(synth)
                raise

            # 将工具执行结果以 tool 角色消息加入 in-context，并持久化
            # tool_call_id 字段将结果与对应的 tool_call 请求关联，LLM 在下一轮推理时能看到每个调用的返回值。
            for tc, result in zip(response.tool_calls, results):
                tool_msg = ToolMessage(
                    content=result.text,
                    tool_call_id=tc["id"],
                )
                self.window.append_and_persist(tool_msg)

            # 终止型工具：submit 类成功提交即本 agent 任务终结——直接
            # 结束 ReAct 循环，不再进下一轮 LLM 调用（否则模型可能反复重复提交同一裁决，
            # 每轮都白烧一遍推理 token）。返回值即提交文本：spawn 的 digest 提取依赖裁决
            # 全文，不能只回一句「已提交」。on_finish 钩子照常执行（安全扫描一致性）。
            if any(r.summary.get(SUMMARY_TERMINAL) for r in results):
                final_text = next(r.text for r in results if r.summary.get(SUMMARY_TERMINAL))
                for mw in self.security_middleware:
                    final_text = await mw.on_finish(self, final_text)
                # 正文来自工具结果而非模型回复,故不挂响应信封——挂上会得到一条
                # finish_reason=tool_calls 的误导性元信息(它不是这次的回答)。
                final = AssistantMessage(content=final_text)
                self.window.append_and_persist(final)
                return final_text

            # 本轮工具调用处理完毕，循环继续（回到开头，将新的上下文送交 LLM 进行下一轮推理）。

        # 安全阀触发：LLM 陷入了无法在限定轮数内退出的循环。
        # 失败残渣随消息逐条落盘（Recall 保留原始记录含失败轮）；MaxTurnsExceeded
        # 只中断本轮回放，不回滚已落盘消息。下轮 window.load() 会看到失败残渣，
        # 由调用方（CLI）决定是否重试。
        raise MaxTurnsExceeded(
            f"ReAct loop did not finish within {self.max_turns} turns"
        )

    def _make_llm_telemetry(self, turn: int):
        """构造 LLM 调用 telemetry 回调（sync，可能跑在线程池线程）。

        每次调用传独立回调（而非共享属性）——同一轮多个 spawn 调用下多个子 agent 共享
        同一个 LLMClient，共享属性会互相覆盖导致归属错乱。实际 fan-out 逻辑在
        _emit_llm_call。

        Args:
            turn: int，当前 ReAct 轮次（绑定到回调，供归属）

        Returns:
            LLM 调用元数据回调（每次调用独立构造，避免并发下互相覆盖归属）。
        """
        return lambda data: self._emit_llm_call(turn, data)

    def _emit_llm_call(self, turn: int, data: dict) -> None:
        """补全归属字段并 fan-out LLM 调用元数据到各中间件 record_llm_call。

        sync（LLM 回调可能跑在线程池线程）。spawn 摘要提取的 LLM 调用也复用
        此入口——父 agent 在做摘要提取,归属父的 trace/session/agent_type/turn。

        Args:
            turn: int，轮次
            data: dict，LLM 客户端给出的元数据（model/tokens/duration 等）
        """
        fields = dict(data)
        fields.update(trace_id=self._trace_id, session_id=self.session_id,
                      agent_type=self.agent_type, turn=turn)
        for mw in self.security_middleware:
            record = getattr(mw, "record_llm_call", None)
            if record is not None:
                record(**fields)

    def _emit_agent_start(self, task: str) -> None:
        """fan-out agent 起跑事件到各中间件（record_agent_start），sync。

        spawn 的子 agent 起跑时也在 spawn 工具的审计 span 内（contextvar 继承），
        故子 agent 的 agent_started 天然嵌在父 spawn 下。mock/测试 llm 没有
        字符串 model/provider，防御性降级为 None。

        Args:
            task: str，本次 run 的任务文本（supervisor=用户原话，子 agent=子任务）
        """
        model = getattr(self.llm, "model", None)
        provider = getattr(self.llm, "provider", None)
        fields = dict(
            trace_id=self._trace_id, session_id=self.session_id,
            agent_type=self.agent_type, task=task,
            agent_version=self._agent_version,
            model=model if isinstance(model, str) else None,
            provider=provider if isinstance(provider, str) else None,
        )
        for mw in self.security_middleware:
            record = getattr(mw, "record_agent_start", None)
            if record is not None:
                record(**fields)

    async def _exec_tool(
        self, tool_call: dict, _confirm_lock: asyncio.Lock | None = None,
        turn: int = 0,
    ) -> ToolResult:
        """执行单个工具调用（薄委托：管道在 ToolExecutor，见 services/tool_executor.py）。

        保留这个签名是因为调用方直接用它：run() 的并发扇出、以及安全测试里的
        大量直调。`tools` / `confirm_callback` / 流式回调在构造后可能被替换，
        故每次现取传进去，而不是让执行器持有它们。

        Args:
            tool_call: LLM 返回的工具调用（function.arguments 为 JSON 字符串）
            _confirm_lock: 同一 message 内并发调用共用的确认串行锁；None = 非并发路径
            turn: ReAct 轮次，注入审计条目供跨轮回溯

        Returns:
            ToolResult，始终返回（不抛异常）
        """
        return await self.tool_executor.execute(
            tool_call, trace_id=self._trace_id, turn=turn,
            confirm_lock=_confirm_lock, tools=self.tools,
            confirm_callback=self.confirm_callback, sink=self.stream_callback)
