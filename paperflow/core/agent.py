# paperflow/core/agent.py
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
- **中间件的拦截不抛异常**：PolicyDenied / ConfirmRequired 等 SecurityError
  被 _exec_tool 捕获并转为带 summary.decision 的 ToolResult
  （policy_denied / user_denied / security_blocked），LLM 在下一轮看到
  决策结果后可自行调整行为
- **only MaxTurnsExceeded 向上抛**：这是唯一"不可恢复"的错误 ——
  LLM 陷入了无法自主退出的循环，需要调用方介入
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from paperflow.core.llm import (
    LLMClient, Message, _message_to_openai, tool_to_openai_schema,
)
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.security import (
    ToolContext, ConfirmRequired, SecurityError, SecurityMiddleware,
)
from paperflow.core.tool import ToolResult
from paperflow.core.security.text import sanitize_surrogates

#: 模块级 logger:意图管线的网络异常/解析失败降级时在此留痕,供运维排查而不是静默吞掉。
logger = logging.getLogger(__name__)


def _intent_block(intent) -> str:
    """把 IntentOutput 格式化为 INTENT 块（ReAct context 的强提示，非命令）。

    排除 clarification 与 prev_intent：澄清只走 CLI 层（跨轮 pending），不暴露给
    Supervisor（避免其用 AskUserTool 双问）；prev_intent 是 conversation 内部状态。
    """
    return "INTENT: " + intent.model_dump_json(exclude={"clarification", "prev_intent"})


def _schema_to_wire(m) -> Message:
    """schemas.Message（Recall 持久化视图）→ wire llm.Message（回放进 in-context 窗口）。

    持久化消息经 MessageManager.get_in_context_messages 加载后,须转回 ReAct 循环
    使用的 wire 格式;role 枚举转字符串,空 content 归一为空串。
    """
    return Message(
        role=m.role.value,
        content=m.content or "",
        tool_calls=m.tool_calls or None,
        tool_call_id=m.tool_call_id,
    )


class MaxTurnsExceeded(Exception):
    """
    ReAct 循环在 max_turns 轮内未产生最终回答时抛出。

    这是 Agent 内置的安全阀 —— 防止 LLM 陷入无限 tool-calling 循环
    （例如 LLM 反复调用同一个工具但不用其结果给出最终回答）。
    调用方（Supervisor 或 CLI）捕获此异常后应终止任务并向用户报告。
    """


@dataclass
class StreamEvent:
    """流式事件：kind ∈ {"content","tool"}；text 为片段；agent_type 区分 root/child。"""
    kind: str
    text: str
    agent_type: str


def _compact(v) -> str:
    """参数值压缩为单行:绝对路径(/ 开头)完整展示,其余值截断到 40 字符。

    路径是文件类工具(read_pdf/write_file 等)的关键信息,截断会让人看不出
    在读哪个文件,故路径不截断。截断处用单个 "…" 标记——三个点语义不清,用户看不出
    内容被截断。len(s) <= 40 时整值展示(40 是完整展示的阈值,不是截断后的长度)。
    """
    s = str(v).replace("\n", " ")
    if s.lstrip().startswith("/"):
        return s
    return s if len(s) <= 40 else s[:37] + "…"


def _format_tool_call(name: str, raw_args: str) -> str:
    """把工具调用格式化为终端一行(claude code 风格:Read(path))。

    尽力解析参数;LLM 产出非法 JSON 或参数缺失时只显示工具名——错误路径保持可读,
    且缓冲清理不依赖参数解析成功(见 _ReplStreamer)。行宽策略:含绝对路径的行不再
    压 80(路径是文件类工具的关键信息,终端可换行展示完整);其余行按"固定前缀后的
    剩余预算"截断参数对;工具名自身过长(预算 ≤0)时退化为纯工具名。
    """
    try:
        args = json.loads(raw_args) if (raw_args or "").strip() else {}
    except json.JSONDecodeError:
        return f"调用 {name}"
    if not isinstance(args, dict) or not args:
        return f"调用 {name}"
    pairs = ", ".join(f"{k}={_compact(v)}" for k, v in args.items())
    if any(str(v).lstrip().startswith("/") for v in args.values()):
        return f"调用 {name}({pairs})"
    budget = max(0, 80 - len(f"调用 {name}()"))
    if budget <= 0:
        return f"调用 {name}()"
    # 行宽截断处补 "…" 标记(留 1 字符给标记),截断后整行仍 ≤80。
    if len(pairs) > budget:
        pairs = pairs[:max(0, budget - 1)] + "…"
    return f"调用 {name}({pairs})"


class Agent:
    """
    ReAct 循环的执行单元，Supervisor 和 SubAgent 共用。

    构造方式（pull 模式）::

        agent = Agent(
            llm=llm_client,
            agent_registry=registry,
            agent_type="searcher",
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
    """

    def __init__(
        self,
        llm: LLMClient,
        agent_registry: AgentRegistry,
        agent_type: str,
        security_middleware: list[SecurityMiddleware] | None = None,
        confirm_callback: Callable[[ConfirmRequired], bool] | None = None,
        intent_enabled: bool = False,
        intent_pipeline=None,      # IntentPipeline | None
        conversation=None,              # ConversationState | None
        ask_user_callback=None,    # Callable[[str], str] | None
        session_id: str | None = None,
        memory=None,                # Memory | None
        agent_manager=None,         # AgentManager | None
        block_manager=None,         # BlockManager | None
        message_manager=None,       # MessageManager | None
        passage_manager=None,       # PassageManager | None
        memory_tools=None,          # list[Tool] | None
        compaction=None,            # CompactionSettings | None
        structured=None,            # StructuredOutput | None
        max_turns: int = 20,
        stream_callback: Callable[[StreamEvent], None] | None = None,
    ):
        """
        :param llm: LLM 客户端实例
        :param agent_registry: Agent 注册表，从中按 agent_type 拉取配置
        :param agent_type: Agent 类型标识符（对应 agents/<agent_type>/ 目录）
        :param security_middleware: 安全中间件列表，按顺序执行 before /
            逆序执行 after；每轮 run 结束时顺序执行 on_finish
        :param confirm_callback: async 确认回调，接收 ConfirmRequired，
            返回 bool；None 时使用 fail-safe 的 _default_confirm（始终拒绝）
        :param intent_enabled: 意图识别门控:仅 CLI 构造的 Supervisor 置 True;
            spawn 工具构造的子 agent 不传管线/会话 → 门控关闭
        :param intent_pipeline: 意图识别管线实例(IntentPipeline | None),
            run() 前置钩子消费;None 时跳过
        :param conversation: 会话状态容器(ConversationState | None),提供跨轮 prev_intent/
            prev_user_input 并在 run 结束后回写
        :param ask_user_callback: 向用户提问的回调(Callable[[str], str] | None),
            供 ask_user 工具消费;None 时该工具不可用
        :param session_id: 会话标识,跨多次 run 保持一致,便于审计聚合;None 时
            自动生成 8 位 hex
        :param memory: Memory 实例(可选),compile() 输出 system 记忆块注入 head
            (每轮重建);None 时跳过
        :param agent_manager: AgentManager 实例(可选),当前仅持有供上层(CLI)取用
        :param block_manager: BlockManager 实例(可选),记忆块 CRUD 的服务句柄
            (记忆工具经它读写核心记忆)
        :param message_manager: MessageManager 实例(可选),对话落盘(Recall) +
            in-context 跨轮回放;None 时记忆相关路径零开销跳过
        :param passage_manager: PassageManager 实例(可选),长期记忆(archival)
            检索服务句柄
        :param memory_tools: 记忆工具列表(list[Tool] | None),框架级注入进
            self.tools 与 _tool_schemas(LLM 通过 function calling 直接读写记忆)
        :param compaction: CompactionSettings 实例(可选),触发时只压缩 in-context
            窗口(驱逐旧对话 + 插摘要),不删 SQL 原始消息
        :param structured: StructuredOutput 实例(可选),compaction 摘要生成路径
            消费;None 时压缩不触发(降级,由 Task 12 CLI 接线)
        :param max_turns: ReAct 循环最大轮数,防止死循环
        :param stream_callback: 流式事件回调(CLI 渲染器消费);None = 非流式路径
            ——run() 保持调 chat(),mock/无 UI 调用方零影响
        """
        # Pull 模式:从唯一注册表按类型加载完整配置
        config = agent_registry.get_config(agent_type)

        #: Agent 注册表(构造子 agent 时需要)
        self.agent_registry = agent_registry

        #: LLM 客户端（async 接口）
        self.llm = llm

        #: Tool 字典，key = tool.name，供 _exec_tool 快速查找
        self.tools = {t.name: t for t in config.tools}

        #: 注入 LLM 的系统提示词，定义本 Agent 的行为规范
        self.system_prompt = config.system_prompt

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
        self.passage_manager = passage_manager
        #: 压缩设置（CompactionSettings | None）：触发时只改 in-context 窗口
        self.compaction = compaction
        #: 结构化输出（StructuredOutput | None）：compaction 摘要生成路径消费
        self.structured = structured

        #: in-context 消息缓冲区（Letta 同款：内部 _messages，外部 messages 只读视图）。
        #: run() 每轮从 MessageManager 重新加载，跨轮消息经 SQL 持久化回放。
        self._messages: list[Message] = []

        #: in-context 窗口的消息 id 追踪（对应 AgentState.message_ids，Letta Message
        #: Buffer）。与 self._messages 并行维护：加载/落盘/压缩都同步。agent_manager
        #: 为 None（无记忆装配）时不参与持久化，窗口回退为「全量回放」。
        self._message_ids: list[str] = []

        #: 当前 run 的追踪 ID，每次 run 开始时重新生成，注入 ToolContext
        self._trace_id: str | None = None

        #: 当前 ReAct 轮次:run() 每轮循环开头更新。spawn 摘要提取的 LLM 调用
        #: 读父 agent 的此属性归属轮次(父在做摘要提取,归父的 trace/轮次)。
        self._current_turn: int = 0

        # 意图识别门控:只有 CLI 构造的 Supervisor 置 True;spawn 工具构造的子 agent
        # 不传管线/会话 → 门控关闭(子任务是结构化任务而非用户意图,跑管线会误分类
        # 且白花 LLM 调用)
        self.intent_enabled = intent_enabled
        self.intent_pipeline = intent_pipeline
        self.conversation = conversation
        self.ask_user_callback = ask_user_callback
        #: 本轮 run 的 IntentOutput（CLI 读 clarification 判定 + 跨轮 prev_intent）
        self.last_intent = None

        # 记忆工具合并进工具面（框架级注入）：LLM 通过 function calling 直接读写
        # 记忆。追加到 self.tools（_exec_tool 查找面）与 _tool_schemas（LLM 工具
        # 描述面），与注册表装载的原子工具同等待遇——needs_parent 默认 False，
        # 无需 attach_agent，故在 opt-in 注入之前放置。
        if memory_tools:
            for t in memory_tools:
                self.tools[t.name] = t
                self._tool_schemas.append(tool_to_openai_schema(t))

        # opt-in 注入：仅对声明 needs_parent 的工具注入父引用。
        # 原子工具不需要 parent；只有嵌套子 agent 的工具声明——权限最小化。
        # 必须放在所有 __init__ 属性赋值之后：attach_agent 可能被工具覆写为
        # 读取父 Agent 属性（如 session_id）的访问器，提前注入则构造期父引用
        # 不完整——被攻陷工具此时读到的 session_id 等仍是缺省值（T1 前瞻坑位）。
        for t in self.tools.values():
            if getattr(t, "needs_parent", False):
                t.attach_agent(self)

    async def _default_confirm(self, cr: ConfirmRequired) -> bool:
        """默认 fail-safe：无人值守时拒绝。"""
        return False

    def _emit(self, ev: StreamEvent) -> None:
        """转发流式事件；无回调时零开销空操作（非 CLI 调用方完全不受影响）。"""
        cb = self.stream_callback
        if cb is not None:
            cb(ev)

    #: messages 只读 property（OpenAI wire 格式视图，Letta 同款）。
    #: 只读：外部（CLI/测试）可观察但不可改，写入统一走 _append_to_messages。
    @property
    def messages(self) -> list[dict]:
        return [_message_to_openai(m) for m in self._messages]

    def _append_to_messages(self, added_messages: "list[Message] | Message") -> None:
        """唯一的消息追加入口（Letta 同名）：把消息追加进 in-context 缓冲区。

        接受单条 Message 或 Message 列表(run() 各分支混用两种风格,测试多用列表),
        统一归一为列表后追加。
        """
        if isinstance(added_messages, Message):
            added_messages = [added_messages]
        self._messages.extend(added_messages)

    def _index_text(self) -> str | None:
        """读取 memory_filesystem.md（MemFS 自动生成的文件树索引）内容。

        索引是渐进暴露的关键：非 system 块不进 compile，但索引让 LLM 知道「有哪些
        文件可读」。无 MemFS 装配（block_manager 无 memfs）时返回 None。
        """
        bm = self.block_manager
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
        （spec §13 块变更即时生效，而非重启才可见）。
        """
        if self.memory is None:
            return None
        if self.block_manager is not None:
            from paperflow.core.memory.schemas.memory import Memory
            self.memory = Memory(blocks=self.block_manager.list_blocks())
        compiled = self.memory.compile(index_text=self._index_text())
        if not compiled:
            return None
        return Message(role="system", content=compiled)

    async def _build_head(self, task: str, force_dispatch: bool = False) -> list[Message]:
        """构建头部 system 消息：① SKILL ② Memory.compile()（system/ 块 + 索引）
        ③ INTENT 块；末尾追加 user task。

        澄清早退时返回单元素 [user] 列表——run() 据此直接返回澄清文本，不落盘、
        不进入 ReAct（澄清是"非任务轮"，只走 CLI 层）。
        """
        head: list[Message] = [Message(role="system", content=self.system_prompt)]
        if self.memory is not None:
            m = self._memory_message()
            if m is not None:
                head.append(m)
        if self.intent_enabled and self.intent_pipeline is not None and self.conversation is not None:
            try:
                intent = await self.intent_pipeline.run(
                    task, prev_intent=self.conversation.prev_intent,
                    prev_user_input=self.conversation.prev_user_input)
            except Exception:
                # 管线失败降级:意图管线是 LLM 兜底,网络异常/解析失败传播到这里——
                # 不阻断本轮:记日志 + 跳过 INTENT 块 + 普通 ReAct 继续。last_intent
                # 显式置 None:CLI 澄清检查跳过、conversation 的上一轮意图不更新。
                logger.warning("intent pipeline failed, degraded to plain ReAct", exc_info=True)
                self.last_intent = None
                intent = None
            if intent is not None:
                self.last_intent = intent
                if intent.clarification and not force_dispatch:
                    # 跨轮澄清:早退在落盘前 → 不持久化(非任务轮)。澄清只走 CLI 层;
                    # INTENT 块不含澄清问题(避免与 ask_user 工具双重发问)。
                    return [Message(role="user", content=intent.clarification)]
                head.append(Message(role="system", content=_intent_block(intent)))
        head.append(Message(role="user", content=task))
        return head

    def _refresh_head_memory(self, head: list[Message]) -> None:
        """会话内刷新 head 里的记忆 system 消息（memory 工具编辑后即时生效）。

        有 block_manager 时在 ReAct 每轮开头重建记忆块并替换 head 中旧的
        <memory_blocks> 消息——同一轮里 memory_replace 改的块,下一轮 LLM 调用即见。
        无记忆装配（memory 或 block_manager 为 None）时零开销空操作。
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

    def _load_in_context(self) -> None:
        """从 MessageManager 加载该会话的 in-context 消息 → self._messages（跨轮回放）。

        message_manager 为 None 时零开销跳过（无记忆装配的子 agent 行为不变）。
        同步记录加载消息的 SQL id 到 self._message_ids，保持与窗口并行——压缩时靠
        它把保留尾部映射回已落盘消息。
        """
        if self.message_manager is None:
            return
        loaded = self.message_manager.get_in_context_messages(self.session_id)
        for m in loaded:
            self._messages.append(_schema_to_wire(m))
        self._message_ids = [m.id for m in loaded]

    def _persist_conversation(self, msgs: list[Message]) -> None:
        """把对话消息全量落盘（Recall）并更新 in-context 窗口追踪。

        message_manager 为 None 时零开销跳过。agent_manager 存在时把落盘消息 id
        追加进 AgentState.message_ids（窗口即「当前 in-context 消息」的持久化视图）。
        """
        if self.message_manager is None:
            return
        added: list[str] = []
        for m in msgs:
            added.append(self.message_manager.add_message(self.session_id, m).id)
        if self.agent_manager is not None:
            for mid in added:
                if mid not in self._message_ids:
                    self._message_ids.append(mid)
            self.agent_manager.update_agent(self.session_id, message_ids=self._message_ids)

    def _persist_compacted_window(self, new_window: list[Message]) -> None:
        """压缩后把窗口状态持久化：摘要落盘 SQL + message_ids 更新为「摘要 + 保留尾部」。

        仅在 agent_manager 存在（窗口状态真正追踪）时生效——无 agent_manager 的
        Agent 是无记忆装配，压缩只改 in-memory 窗口，SQL 保持原样（兼容既有测试语义）。
        尾部消息来自压缩前的窗口（已落盘，id 在 _message_ids 里）；摘要消息是新生成
        的，先 add_message 取 id。被驱逐旧消息只移出窗口，不删 SQL（Recall 完整）。
        """
        if self.agent_manager is None:
            return
        old_id_by_msg = {id(m): mid for m, mid in zip(self._messages, self._message_ids)}
        new_ids: list[str] = []
        for m in new_window:
            mid = old_id_by_msg.get(id(m))
            if mid is None:
                mid = self.message_manager.add_message(self.session_id, m).id
            new_ids.append(mid)
        self._message_ids = new_ids
        self.agent_manager.update_agent(self.session_id, message_ids=new_ids)

    def _needs_compaction(self, messages: list[Message]) -> bool:
        """in-context 消息是否超压缩阈值（只检查，执行在 run() 里）。

        依赖 message_manager 存在:无持久化层时压缩无从安置(摘要无处回放),跳过。
        """
        from paperflow.core.memory.compaction import should_compress
        if self.compaction is None or self.message_manager is None:
            return False
        return should_compress(messages, self.compaction, self.llm.context_window)

    async def run(self, task: str, *, force_dispatch: bool = False) -> str:
        """
        执行 ReAct 循环，返回 LLM 的最终文本回答。

        这是 Agent 的唯一公共入口。调用方（CLI、Supervisor 的 SpawnSubAgentTool）
        只需要传入任务文本，等待返回结果。

        :param task: 用户任务文本（对于 Supervisor 是原始用户输入；
                     对于 SubAgent 是 Supervisor 拆分后的子任务）
        :param force_dispatch: 强制调度开关（跨轮澄清 ≤2 轮终止路径）——
            置 True 时即使管线产出 clarification 也跳过早退，直接跑 ReAct
        :returns: LLM 的最终文本回答（经过所有中间件的 on_finish 钩子改写）
        :raises MaxTurnsExceeded: 超过 max_turns 轮仍未停止

        ReAct 循环步骤::

            1. 生成本次 run 的 trace_id（trace_<12位hex）并清洗 task 的未配对 surrogate
            2. 构建 head：① system_prompt → ② Memory.compile()（system/ 记忆块，
               若有）→ ②b INTENT 块（intent_enabled 且管线成功时）→ user_task。
               澄清早退直接返回澄清文本（不落盘、不进入 ReAct）
            3. 从 MessageManager 加载该会话的 in-context 消息（跨轮回放），当前
               user task 落盘；消息归属 self._messages（in-context 窗口）
            4. 调用 LLM 前检查压缩（compaction.should_compress → run_compaction
               驱逐旧对话 + 插摘要，只改 in-context 窗口、不删 SQL），随后调用 LLM
            5. 如果无 tool_calls → 顺序执行各中间件的 on_finish 钩子，最终回答
               落盘后返回改写后的 content（LLM 判定任务完成）
            6. 如果有 tool_calls → 并发执行（gather，结果按调用顺序返回），将
               ToolResult 落盘并附加到 in-context 消息
            7. 回到步骤 4，LLM 根据工具执行结果继续推理
            8. 若超过 max_turns → 抛出 MaxTurnsExceeded（安全阀）
        """
        # 每次 run 独立追踪 ID：同一 conversation 的多次 run 由 trace_id 区分
        self._trace_id = f"trace_{uuid.uuid4().hex[:12]}"

        # 信任边界：清洗用户输入里的未配对 surrogate（外部粘贴/合成文本可能携带，
        # 见 core/security/text.py）——否则下游意图管线/实体提取在脏字符上工作，且
        # conversation.prev_user_input 会把脏字符带入下一轮。正常输入零开销（无匹配回原串）。
        task = sanitize_surrogates(task)

        # head:① SKILL ② Memory.compile()(system/ 记忆块) ③ INTENT 块,每轮重建
        # 不进累积;末尾 user task。澄清早退时 head=[user 澄清文本] → 直接返回,
        # 不落盘不加载(澄清是"非任务轮",只走 CLI 层)。
        head = await self._build_head(task, force_dispatch=force_dispatch)
        if len(head) == 1 and head[0].role == "user":
            return head[0].content

        #: in-context 窗口每轮重建:跨轮回放统一经 MessageManager(SQL) 加载,避免
        #: self._messages 跨 run 残留导致下一轮重复加载(Letta 同款:每步重新 load)。
        self._messages = []
        self._load_in_context()
        #: 当前 user task 落盘(Recall),下轮经 _load_in_context 回放
        self._persist_conversation([head[-1]])

        #: 截断续写累积器：半截回答在此暂存，完整回答返回前合并。
        #: 只在截断→续写场景使用；非截断路径保持空列表，零额外行为。
        accumulated: list[str] = []

        for turn in range(self.max_turns):
            # 记录当前轮次:spawn 摘要提取的 LLM 调用据此归属父 agent 的当前轮
            self._current_turn = turn
            # 记忆块会话内即时生效:memory 工具编辑后,下一轮 LLM 调用即见新块
            # (spec §13;head 是本地列表,_refresh_head_memory 就地替换记忆消息)。
            self._refresh_head_memory(head)
            #: LLM 输入 = head 前段(system/memory/INTENT) + in-context 回放历史 +
            #: 末尾当前 user task(恒末位,与旧版「历史在前、当前任务在末」一致——否则
            #: LLM 会把回放历史里的旧任务误当当前任务)。
            messages = list(head[:-1]) + self._messages + [head[-1]]
            # 压缩检查:只改 in-context 窗口(驱逐旧对话 + 插摘要),SQL 原始消息由
            # MessageManager 保留(Recall 可追溯)。structured 未注入(摘要生成器缺失)
            # 时压缩不触发——避免 None.extract 崩溃,Task 12 CLI 接线后恢复。
            if self._needs_compaction(messages) and self.structured is not None:
                # 截断续写与压缩重建互斥:压缩可能驱逐"半截+续写提示"(in-context
                # 重建),先弃掉累积器里的半截——续写无参照即完整重答,避免
                # 「半截 + 完整重答」重复交付(与旧版 accumulated.clear 语义一致)。
                accumulated.clear()
                from paperflow.core.memory.compaction import run_compaction
                new_window = await run_compaction(
                    self._messages, self.compaction, self.llm, self.structured)
                # 摘要落盘 + message_ids 更新为「摘要 + 保留尾部」——压缩产物跨轮
                # 持久,下轮 _load_in_context 按 message_ids 回放摘要、不回放被驱逐
                # 旧消息(评审 I-3;SQL 原始消息仍全量保留,Recall 完整)。
                self._persist_compacted_window(new_window)
                self._messages = new_window
                messages = list(head[:-1]) + self._messages + [head[-1]]

            # 流式门控：挂了 stream_callback 才走 chat_stream（否则保持 chat()）。
            # mock LLM 只有 chat 方法，无条件换 chat_stream 会让 MagicMock 不可
            # await 抛 TypeError——门控同时是零开销路径（无 UI 调用方不受影响）。
            tools = self._tool_schemas if self._tool_schemas else None
            if self.stream_callback is not None:
                response = await self.llm.chat_stream(
                    messages, tools=tools,
                    on_delta=lambda d: self._emit(
                        StreamEvent("content", d, self.agent_type)),
                    telemetry_callback=self._make_llm_telemetry(turn),
                )
            else:
                response = await self.llm.chat(
                    messages, tools=tools,
                    telemetry_callback=self._make_llm_telemetry(turn))

            # LLM 判定任务完成：返回无 tool_calls 的纯文本消息
            if not response.tool_calls:
                if response.truncated:
                    # 内容被截断 → 不当作最终回答返回(否则静默交付残缺内容)。暂存半截、
                    # 把已生成部分+续写提示放进 in-context,继续循环;max_turns 天然封顶
                    # 续写次数,不会死循环。
                    accumulated.append(response.content or "")
                    self._append_to_messages(response)
                    self._append_to_messages(Message(
                        role="user",
                        content="上一条回答因输出长度上限被截断，请直接从断点继续输出，不要重复已输出的内容。"))
                    self._persist_conversation([response])
                    continue
                content = "".join(accumulated) + (response.content or "")
                accumulated.clear()
                # on_finish 钩子：顺序执行，可逐级改写最终回答
                # （如追加来源引用、注入安全声明等）
                for mw in self.security_middleware:
                    content = await mw.on_finish(self, content)
                # 意图会话更新:本轮消费了 intent → 更新上一轮意图/输入供下轮追问使用
                # (澄清早退或管线降级时 last_intent 为 None → 不更新)
                if self.intent_enabled and self.last_intent is not None:
                    self.conversation.prev_intent = self.last_intent.intent_type
                    self.conversation.prev_user_input = task
                # 最终回答(经 on_finish 改写——回放给下轮的是"用户看到的事实",
                # SAFE_PROMPT 等安全声明跨轮保留)落盘 + 进 in-context,供下轮回放
                final = Message(role="assistant", content=content)
                self._append_to_messages(final)
                self._persist_conversation([final])
                return content

            # LLM 请求调用工具：将 assistant 消息（含 tool_calls）加入 in-context + 落盘
            self._append_to_messages(response)
            self._persist_conversation([response])

            # 并发执行 LLM 请求的工具调用:同一 message 的多个工具调用用 gather 并行
            # (工具已在线程池执行,真实并发不阻塞事件循环)。gather 按输入顺序返回 →
            # 结果顺序与工具调用 ID 映射不变,LLM 关联结果到对应调用的顺序语义不因并发
            # 而改变。并发上限 4(信号量)防一次性打爆网络源;确认用锁串行(CLI 标准输入
            # 并发读会竞态)。
            sem = asyncio.Semaphore(4)
            confirm_lock = asyncio.Lock()

            async def _run_one(tc: dict) -> ToolResult:
                async with sem:
                    return await self._exec_tool(
                        tc, _confirm_lock=confirm_lock, turn=turn)

            results = await asyncio.gather(*(_run_one(tc) for tc in response.tool_calls))

            # 将工具执行结果以 tool 角色消息加入 in-context + 落盘
            # tool_call_id 将这条结果关联到 LLM 请求的对应 tool_call
            for tc, result in zip(response.tool_calls, results):
                tool_msg = Message(
                    role="tool",
                    content=result.text,
                    tool_call_id=tc["id"],
                )
                self._append_to_messages(tool_msg)
                self._persist_conversation([tool_msg])

        # 安全阀触发：LLM 陷入了无法在限定轮数内退出的循环。
        # 失败残渣随消息逐条落盘（Recall 完整原始记录，含失败轮——Letta 语义）；
        # MaxTurnsExceeded 只中断本轮回放，不回滚已落盘消息。下轮 _load_in_context
        # 会看到失败残渣，由调用方（CLI）决定是否重试。
        raise MaxTurnsExceeded(
            f"ReAct loop did not finish within {self.max_turns} turns"
        )

    def _make_llm_telemetry(self, turn: int):
        """构造 LLM 调用 telemetry 回调（sync，可能跑在线程池线程）。

        每次调用传独立回调（而非共享属性）——parallel_spawn 下多个子 agent 共享
        同一个 LLMClient，共享属性会互相覆盖导致归属错乱。实际 fan-out 逻辑在
        _emit_llm_call。
        """
        return lambda data: self._emit_llm_call(turn, data)

    def _emit_llm_call(self, turn: int, data: dict) -> None:
        """补全归属字段并 fan-out LLM 调用元数据到各中间件 record_llm_call。

        sync（LLM 回调可能跑在线程池线程）。spawn 摘要提取的 LLM 调用也复用
        此入口——父 agent 在做摘要提取,归属父的 trace/session/agent_type/turn。
        """
        fields = dict(data)
        fields.update(trace_id=self._trace_id, session_id=self.session_id,
                      agent_type=self.agent_type, turn=turn)
        for mw in self.security_middleware:
            record = getattr(mw, "record_llm_call", None)
            if record is not None:
                record(**fields)

    async def _exec_tool(
        self, tool_call: dict, _confirm_lock: asyncio.Lock | None = None,
        turn: int = 0,
    ) -> ToolResult:
        """
        执行单个 LLM 请求的工具调用，内部处理所有异常。

        流程(中间件管道)::

            1. 构造 ToolContext（trace_id / session_id / agent_type / 工具 / 参数）
               —— ctx 在参数解析前构造，保证所有路径都能走 after 链审计
            2. 解析 JSON 参数（失败 → 走 after 链 → 错误 ToolResult）
            3. 非 dict 参数归一化为 {}（防止 ** 展开崩溃，审计记录空参数）
            4. 未知工具（不存在 → 走 after 链 → 错误 ToolResult）
            5. before 阶段：顺序执行各中间件的 before 钩子
               - 抛 ConfirmRequired → 调用 confirm_callback 决策：
                 拒绝 → user_denied ToolResult；通过 → 执行工具
               - 抛其他 SecurityError → policy_denied / security_blocked ToolResult
            6. 执行工具（异常 → ToolResult(text="Tool error: ...")）
            7. after 阶段：逆序执行各中间件的 after 钩子（洋葱模型）

        注意:JSON 解析失败和未知工具不绕过中间件管道——ctx 在解析前构造,
        早退路径也走 after 链(仅审计),保证这些异常路径同样留下审计痕迹
        (工具为 None 时各中间件 before 钩子不执行,只有审计记录调用)。

        错误处理采用"降级为文本"策略:所有异常(JSON 解析失败、未知工具名、工具
        执行异常、中间件拦截)都转为 ToolResult(text="..."),作为正常对话流的一部分
        反馈给 LLM。LLM 在下一轮中看到错误文本后可自行决定重试、调整参数或放弃。

        :param tool_call: LLM 返回的工具调用字典
            {"id": str, "function": {"name": str, "arguments": str}}
            其中 arguments 为 JSON 字符串，此方法负责 json.loads 解析
        :param _confirm_lock: 并发确认串行锁(asyncio.Lock | None)。同一 message 的
            多个工具调用并发执行时由 run() 传入同一个锁,把确认回调调用串行化
            (CLI 标准输入并发读会竞态);None = 非并发路径,确认行为与现状一致
        :param turn: ReAct 轮次,注入审计条目供跨轮回溯;run() 每轮透传,
            直接调用 _exec_tool 时默认 0
        :returns: ToolResult，始终返回（不抛异常）
        """
        name = tool_call["function"]["name"]

        # 工具事件放解析前：即使后续 JSON 解析失败 / 未知工具 / 被中间件拦截，
        # root 的中间内容缓冲也要被清掉（_ReplStreamer 依赖），否则 should_print
        # 会把中间思考文本误当最终答案。
        # 门控：stream_callback 为 None（非 CLI 调用方）时连 _format_tool_call 的
        # json.loads 也不做——保持“无回调零开销空操作”不变式。
        if self.stream_callback is not None:
            self._emit(StreamEvent("tool", _format_tool_call(
                name, tool_call["function"]["arguments"]), self.agent_type))

        # 1. 按工具名查找 Tool 实例（可能 None = 未知工具，LLM 幻觉/注入）
        tool = self.tools.get(name)

        # 2. 构建工具调用的上下文对象，供中间件读写
        #    （提前构造：JSON 解析失败 / 未知工具也要走 after 链审计）
        ctx = ToolContext(
            trace_id=self._trace_id,
            session_id=self.session_id,
            agent_type=self.agent_type,
            tool=tool,
            tool_name=name,
            timestamp=datetime.now().isoformat(),
            started_at=time.monotonic(),
            turn=turn,
        )

        # 3. 解析 LLM 生成的 JSON 参数字符串
        try:
            raw_args = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError as e:
            # LLM 生成了非法 JSON → 走 after 链记录审计后反馈给 LLM
            ctx.error = e
            await self._run_after_hooks(ctx)
            return ToolResult(text=f"Tool argument parse error: {e}")

        # 4. 非 dict 参数（数组/字符串等）归一化为 {}，防止 ** 展开崩溃
        ctx.args = raw_args if isinstance(raw_args, dict) else {}

        # 5. 未知工具（LLM 幻觉或 prompt injection）→ 走 after 链审计后反馈可用工具列表
        if tool is None:
            ctx.error = ValueError(f"Unknown tool: {name}")
            await self._run_after_hooks(ctx)
            return ToolResult(
                text=f"Unknown tool: {name}. Available: {list(self.tools.keys())}"
            )

        # 6. before 阶段：中间件按顺序放行 / 拦截
        for mw in self.security_middleware:
            try:
                await mw.before(ctx)
            except ConfirmRequired as cr:
                # 高风险操作:交由确认回调决策。并发时多个工具调用的确认回调都跑在事件
                # 循环线程上(to_thread 只包工具执行,中间件/回调不离开事件循环),但并行
                # gather 让它们在 await 点交错——CLI 标准输入并发读会竞态(两个回调同时
                # 抢 input())。确认锁把确认决策串行化:一个确认未决时其余等待;非并发路径
                # (锁为 None,单工具调用)行为与现状完全一致。
                # 审批请求事件:发起确认前先落盘(请求≠决策,两条独立事件供合规回溯)。
                for mw in self.security_middleware:
                    await mw.on_approval(ctx, "requested")
                async def _decide() -> bool:
                    return await self.confirm_callback(cr)
                if _confirm_lock is not None:
                    async with _confirm_lock:
                        confirmed = await _decide()
                else:
                    confirmed = await _decide()
                # 决策语义:确认通过 → user_confirmed;被拒时按是否有真实人工回调区分
                # user_denied / auto_denied(fail-safe 默认拒绝,无人值守)。
                outcome = (
                    "user_confirmed" if confirmed else (
                        "user_denied" if self._has_human_confirm else "auto_denied"))
                ctx.approval_outcome = outcome
                for mw in self.security_middleware:
                    await mw.on_approval(ctx, "decided", approval_outcome=outcome)
                if not confirmed:
                    # 拒绝 → 记录错误并走 after 钩子，反馈给 LLM
                    # summary.decision 用已计算的 outcome（user_denied/auto_denied），
                    # 与 ctx.approval_outcome 保持一致——拒绝路径带决策依据回放给 LLM
                    ctx.error = cr
                    await self._run_after_hooks(ctx)
                    return ToolResult(
                        text=f"User denied: {cr.tool_name}",
                        summary={"decision": outcome, "tool": cr.tool_name},
                    )
                cr.confirm()
                ctx.user_confirmed = True
            except SecurityError as se:
                # 策略拦截（policy_denied / security_blocked）→ 带决策摘要返回
                ctx.error = se
                await self._run_after_hooks(ctx)
                return ToolResult(
                    text=f"{se.decision}: {se.reason}",
                    summary={"decision": se.decision, "violations": getattr(se, "violations", [])},
                )

        # 7. 执行工具逻辑（结果统一规范化为 ToolResult）
        try:
            # CPU/网络密集型工具在线程池执行,避免阻塞事件循环——否则并行派发与后台
            # 归档任务会被单个工具调用卡死。
            # opt-in 注入每轮搜索状态:声明 wants_run_state 的搜索类工具拿到按追踪 ID
            # 键控的同一个去重池,跨多次工具调用共享自动去重;未声明的工具零开销。
            # 注入方式:作为 to_thread 的独立参数直接传 execute,**不写进 ctx.args**——
            # ctx.args 会被 after 钩子/审计读取并序列化,而去重池不可序列化,写进去
            # 会让该调用的审计行整体丢失。
            if getattr(tool, "wants_run_state", False):
                from paperflow.tools.search._common import get_run_state
                raw = await asyncio.to_thread(
                    tool.execute, **ctx.args, _run_state=get_run_state(self._trace_id))
            else:
                raw = await asyncio.to_thread(tool.execute, **ctx.args)
            ctx.result = raw if isinstance(raw, ToolResult) else ToolResult(text=str(raw))
        except Exception as e:
            # 工具执行失败（网络超时、文件不存在等）→ 反馈给 LLM
            ctx.error = e
            ctx.result = ToolResult(text=f"Tool error: {e}")

        # 8. after 阶段（逆序 = 洋葱模型，后注册的中间件先看到结果）
        await self._run_after_hooks(ctx)
        return ctx.result

    async def _run_after_hooks(self, ctx: ToolContext) -> None:
        """
        逆序执行所有中间件的 after 钩子（洋葱模型）。

        无论工具是否执行成功、无论是否被 before 拦截，
        只要进入了管道（ctx 已构建）就会执行 after，
        保证审计等横切关注点在所有路径上都能记录。
        """
        for mw in reversed(self.security_middleware):
            try:
                await mw.after(ctx)
            except Exception as e:
                # 审计等 after 钩子失败不应中止工具执行结果返回
                print(f"[security] after hook {type(mw).__name__} failed: {e}", file=sys.stderr)
