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
from paperflow.core.skill_registry import SkillRegistry
from paperflow.core.security import (
    ToolContext, ConfirmRequired, SecurityError, SecurityMiddleware,
)
from paperflow.core.tool import ToolResult
from paperflow.core.security.text import sanitize_surrogates

#: 模块级 logger:意图管线的网络异常/解析失败降级时在此留痕,供运维排查而不是静默吞掉。
logger = logging.getLogger(__name__)

#: 同路径写/编辑串行锁注册表（键 = 目标文件路径）。真实会话复验发现：同一 message
#: 并行发两个 edit_file 改同一文件时，双方都在对方决策前弹确认（「a」授权只覆盖
#: 先到者），且并发读改写同一文件有丢写竞态——requires_confirm 的写类工具按路径
#: 加锁串行化，后到者等前者完整走完确认+执行，授权键已入集合则不再弹框。
_path_locks: dict[str, asyncio.Lock] = {}


def _path_lock(path: str) -> asyncio.Lock:
    """取目标路径的串行锁（无则建；setdefault 原子，多循环场景安全）。"""
    return _path_locks.setdefault(path, asyncio.Lock())


def _intent_block(intent) -> str:
    """把 IntentOutput 格式化为 INTENT 块（ReAct context 的强提示，非命令）。

    排除 clarification 与 prev_intent：澄清只走 CLI 层（跨轮 pending），不暴露给
    Supervisor（避免其用 AskUserQuestionTool 双问）；prev_intent 是 conversation 内部状态。
    """
    return "INTENT: " + intent.model_dump_json(exclude={"clarification", "prev_intent"})


#: 取消路径合成的 tool 消息（历史自愈）。自解释措辞：真实会话复验发现，裸的
#: "cancelled" 会让模型把中断编造成「子任务失败/被外部打断」等错误叙述——
#: 这里明确因果（用户主动 Ctrl+C）并禁止错误归因。
_CANCELLED_TOOL_MSG = (
    '{"decision":"cancelled",'
    '"reason":"用户主动中断(Ctrl+C)，本轮工具调用作废。'
    '这不是子任务失败或超时，请勿将中断归因于其他原因。"}'
)


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
    """参数值压缩为单行：头尾中间截断，超长标注字符数。

    路径(/ 开头)在行宽预算内也头尾截断——超长路径全展示会被终端宽度硬切
    (overflow 兜底见渲染层)，头尾各留一段可辨认。截断统一用 "…" 标记。
    """
    s = str(v).replace("\n", " ")
    n = len(s)
    if n <= 40:
        return s
    if n <= 120:
        return s[:35] + "…" + s[-10:]
    return s[:40] + "…(%d chars)…" % n + s[-20:]


def _format_tool_call(name: str, raw_args: str) -> str:
    """把工具调用格式化为终端一行(如 Calling Read(path))。

    尽力解析参数;LLM 产出非法 JSON 或参数缺失时只显示工具名——错误路径保持可读,
    且缓冲清理不依赖参数解析成功(见 terminal.render.StreamRenderer)。每个值经
    _compact 头尾截断,超长自动标注字符数。含路径参数(值以 / 开头)的行豁免行宽
    预算:超长路径已头尾截断,再被 80 列切一刀会把文件名尾部切没——宽度交给渲染层
    overflow="fold" 兜底(见 _compact 与 terminal.render 的溢出说明)。
    """
    try:
        args = json.loads(raw_args) if (raw_args or "").strip() else {}
    except json.JSONDecodeError:
        return f"Calling {name}"
    if not isinstance(args, dict) or not args:
        return f"Calling {name}"
    pairs = ", ".join(f"{k}={_compact(v)}" for k, v in args.items())
    # 路径参数豁免行宽预算:返回 _compact 后的完整 pairs,尾部(文件名)存活。
    if any(str(v).lstrip().startswith("/") for v in args.values()):
        return f"Calling {name}({pairs})"
    budget = max(0, 80 - len(f"Calling {name}()"))
    if budget <= 0:
        return f"Calling {name}()"
    # 行宽截断处补 "…" 标记(留 1 字符给标记),截断后整行仍 ≤80。
    if len(pairs) > budget:
        pairs = pairs[:max(0, budget - 1)] + "…"
    return f"Calling {name}({pairs})"


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
        compaction=None,            # CompactionSettings | None
        structured=None,            # StructuredOutput | None
        max_turns: int = 20,
        stream_callback: Callable[[StreamEvent], None] | None = None,
        skill_registry: SkillRegistry | None = None,   # Skill 注册表；None = 无 skill 体系
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
            供 ask_user_question 工具消费;None 时该工具不可用
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
        :param compaction: CompactionSettings 实例(可选),触发时只压缩 in-context
            窗口(驱逐旧对话 + 插摘要),不删 SQL 原始消息
        :param structured: StructuredOutput 实例(可选),compaction 摘要生成路径
            消费;None 时压缩不触发(降级,CLI 接线后恢复)
        :param max_turns: ReAct 循环最大轮数,防止死循环
        :param stream_callback: 流式事件回调(CLI 渲染器消费);None = 非流式路径
            ——run() 保持调 chat(),mock/无 UI 调用方零影响
        :param skill_registry: Skill 注册表(可选)。提供时按 agent_type 计算 L1
            <available_skills> 清单块注入 head(静态,每轮重建 head 时原样携带);
            None 时整块省略零开销
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

        #: Skill 注册表（spawn 构造子 agent 时透传用）
        self.skill_registry = skill_registry

        #: L1 <available_skills> 清单块（静态；空串 = 无可见 skill，head 整块省略）
        self.skills_block = skill_registry.skills_block(agent_type) if skill_registry else ""

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

        #: in-context 消息缓冲区（内部 _messages 是真实缓冲区，外部 messages 是
        #: 只读 wire 视图）。run() 每轮从 MessageManager 重新加载，跨轮消息经 SQL
        #: 持久化回放，不在内存里跨 run 累积。
        self._messages: list[Message] = []

        #: in-context 窗口的消息 id 追踪（对应 AgentState.message_ids）。与
        #: self._messages 并行维护：加载/落盘/压缩都同步。agent_manager 为 None
        #: （无记忆装配）时不参与持久化，窗口回退为「全量回放」。
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

        # opt-in 注入：仅对声明 needs_parent 的工具注入父引用。
        # 原子工具不需要 parent；只有嵌套子 agent 的工具声明——权限最小化。
        # 必须放在所有 __init__ 属性赋值之后：attach_agent 可能被工具覆写为
        # 读取父 Agent 属性（如 session_id）的访问器，提前注入则构造期父引用
        # 不完整——被攻陷工具此时读到的 session_id 等仍是缺省值（安全前瞻坑位）。
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

    #: messages 只读 property（OpenAI wire 格式视图）。
    #: 只读：外部（CLI/测试）可观察但不可改，写入统一走 _append_to_messages。
    @property
    def messages(self) -> list[dict]:
        return [_message_to_openai(m) for m in self._messages]

    def _append_to_messages(self, added_messages: "list[Message] | Message") -> None:
        """唯一的消息追加入口：把消息追加进 in-context 缓冲区。

        接受单条 Message 或 Message 列表(run() 各分支混用两种风格,测试多用列表),
        统一归一为列表后追加。
        """
        # 判断 added_messages 这个变量是否是 Message 类（或其子类）的实例。
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

        # compile() 只渲染 persona/human 两块 + 文件树索引（渐进暴露，其余块按需读）。compiled 的实际内容长这样：
        #   <memory_blocks>
        #   <block name="persona">…助手身份设定…</block>     ← 只有 persona/human 两块的内容
        #   <block name="human">…用户画像…</block>
        #   </memory_blocks>
        #   <memory_filesystem>
        #   …memory_filesystem.md 的内容…                    ← 文件树索引（文件名/结构，不是正文）
        #   </memory_filesystem>
        compiled = self.memory.compile(index_text=self._index_text())
        if not compiled:
            return None
        return Message(role="system", content=compiled)

    async def _build_head(self, task: str, force_dispatch: bool = False) -> list[Message]:
        """构建本轮 ReAct 循环的头部消息列表（system 层 + 用户任务）。

        此方法在每个 ReAct 轮次开始时被调用，用于组装 LLM 输入的前置部分（system 消息）。
        它按顺序拼接五块内容：
            1. system: AGENT.md 系统提示（来自 agent 配置，定义角色与行为规范）
            2. system: SKILLS 清单块（L1 渐进披露清单，若装配了 SkillRegistry 且有可见 skill）
            3. system: 记忆块（Memory.compile() 输出的 persona/human + 文件树索引，若有）
            4. system: 意图识别块（若启用意图管线且管线成功，格式化为 system 消息的 INTENT 块）
            5. 末尾追加 user task。

        特殊路径：若意图管线返回了 clarification（澄清问题）且 force_dispatch=False，
        则直接返回 [user: clarification]（单元素列表），以此通知 run() 跳过 ReAct 循环，
        将澄清问题直接返回给调用方（CLI 层），实现跨轮澄清。

        Args:
            task: 本轮用户输入文本（原始任务）。
            force_dispatch: 强制调度标志。若为 True，即使意图管线要求澄清，也跳过早退，
                继续执行 ReAct（用于跨轮澄清超过 2 轮后的强制终止路径）。

        Returns:
            list[Message]: 头部消息列表。正常返回 [system_prompt, skills(可选), memory(可选), intent(可选), user_task]；
                澄清早退时返回 [user(clarification)]，长度仅为 1 且 role 为 user。
        """
        # ====== 第1层：AGENT.md 系统提示 ======
        head: list[Message] = [Message(role="system", content=self.system_prompt)]

        # ====== 第2层：SKILLS 清单（L1 渐进披露，静态） ======
        # skill 指令的约束力声明写在块内；无可见 skill 时 skills_block 为空串，整块省略
        if self.skills_block:
            head.append(Message(role="system", content=self.skills_block))

        # ====== 第3层：记忆块（核心记忆 + 文件系统索引） ======
        if self.memory is not None:
            m = self._memory_message()
            if m is not None:
                head.append(m)

        # ====== 第4层：意图识别块 ======
        # 若管线返回 clarification，则表明当前输入意图不明确，需要向用户追问。
        if self.intent_enabled and self.intent_pipeline is not None and self.conversation is not None:
            try:
                # 调用意图管线，传入上一轮意图和输入（用于追问检测）
                intent = await self.intent_pipeline.run(
                    task, prev_intent=self.conversation.prev_intent,
                    prev_user_input=self.conversation.prev_user_input)
            except Exception:
                # 管线失败（如 LLM 调用超时）：降级处理，不阻断主流程，
                # 不阻断本轮:记日志 + 跳过 INTENT 块 + 普通 ReAct 继续。
                # last_intent 显式置 None:CLI 澄清检查跳过、conversation 的上一轮意图不更新。
                logger.warning("intent pipeline failed, degraded to plain ReAct", exc_info=True)
                self.last_intent = None
                intent = None

            if intent is not None:
                # ---------- 跨轮澄清早退路径 ----------
                # 如果意图管线返回了 clarification 字段（即需要向用户提问）
                # 且 force_dispatch 未置 True，则不走 ReAct，而是直接返回澄清问题
                # 作为用户消息。run() 检测到 head 长度为 1 且 role 为 user 时，
                # 会直接返回该文本，不落盘、不进入工具循环。
                # 这样，本轮对话实际上是一个“非任务轮”，CLI 层将问题展示给用户，
                # 等待用户回答后重新调用 run()，实现跨轮澄清（最多 2 轮）。
                self.last_intent = intent
                if intent.clarification and not force_dispatch:
                    # 跨轮澄清:早退在落盘前 → 不持久化(非任务轮)。澄清只走 CLI 层;
                    # INTENT 块不含澄清问题(避免与 ask_user_question 工具双重发问)。
                    return [Message(role="user", content=intent.clarification)]

                # 正常路径：将意图结果序列化为 INTENT 块，注入 system 消息，
                # 让 LLM 在执行任务时获得路由先验。
                head.append(Message(role="system", content=_intent_block(intent)))

        # ====== 第5层：用户任务 ======
        # 最后将当前用户输入作为 user 消息追加。
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

        # 从 MessageManager 加载当前会话的 in-context 消息（跨轮回放）。
        # 内部逻辑：先读 AgentState.message_ids 确定窗口范围，
        #   - 有 message_ids → 按 id 精确查询，返回压缩后的窗口（摘要+保留尾部）
        #   - 无 message_ids → 降级全量查询该 agent 所有消息（首轮/未压缩兼容）
        # 返回 schemas.Message 列表，后续经 _schema_to_wire 转为 wire 格式追加进 self._messages。
        loaded = self.message_manager.get_in_context_messages(self.session_id)
        for m in loaded:
            self._messages.append(_schema_to_wire(m))
        self._message_ids = [m.id for m in loaded]
        self._patch_orphan_history()

    def _patch_orphan_history(self) -> None:
        """回放窗口校验：为孤儿 tool_calls 就地补合成 tool 消息（历史自愈兜底）。

        历史数据里可能存在 assistant(tool_calls) 无配对 tool 消息的坏记录
        （历史版本在取消路径直接崩溃、没来得及合成消息），原样回放给 OpenAI
        会报 400「tool_calls 后必须紧跟 tool 消息」。这里扫描整个窗口，对每个
        未被响应的 tool_call 在其 assistant 消息后补一条 ``{"decision":
        "cancelled"}`` 的 tool 消息，并落盘保持窗口 id 对齐——压缩依赖
        _messages 与 _message_ids 严格平行，插入时必须同步维护两边。
        """
        if self.message_manager is None or not self._messages:
            return
        answered = {
            m.tool_call_id for m in self._messages
            if m.role == "tool" and m.tool_call_id
        }
        new_msgs: list[Message] = []
        new_ids: list[str] = []
        changed = False
        for m, mid in zip(self._messages, self._message_ids):
            new_msgs.append(m)
            new_ids.append(mid)
            tcs = m.tool_calls or []
            missing = [
                tc for tc in tcs
                if isinstance(tc, dict) and tc.get("id") and tc["id"] not in answered
            ]
            if missing:
                changed = True
                for tc in missing:
                    synth = Message(
                        role="tool",
                        content=_CANCELLED_TOOL_MSG,
                        tool_call_id=tc["id"],
                    )
                    new_ids.append(self.message_manager.add_message(self.session_id, synth).id)
                    new_msgs.append(synth)
        if changed:
            self._messages = new_msgs
            self._message_ids = new_ids
            if self.agent_manager is not None:
                self.agent_manager.update_agent(self.session_id, message_ids=new_ids)

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

        # 将当前 in‑context 窗口中的每条 Message 对象（内存地址）关联到它在 SQL 数据库中的持久化 ID。old_id_by_msg 的 key 为 id(m)（内存地址），值为 message_id
        # 因为压缩后的新窗口 new_window 包含：1、旧的“保留尾部”消息（这些对象本身可能来自原 _messages）2、新生成的摘要消息（Message 对象，尚未落盘）
        # 为了更新 self._message_ids（即持久化的窗口 ID 列表），需要知道哪些消息已经落盘到 AgentState 的 message_ids，哪些是新生成的摘要（需要调用 add_message 插入）。
        old_id_by_msg = {id(m): mid for m, mid in zip(self._messages, self._message_ids)}

        new_ids: list[str] = []
        for m in new_window:
            # 尝试从旧映射中查找此消息是否已落盘
            mid = old_id_by_msg.get(id(m))

            # 若是新生成的摘要消息，则需插入 SQL 并获得新 ID
            if mid is None:
                mid = self.message_manager.add_message(self.session_id, m).id
            new_ids.append(mid)

        # 更新内存中的窗口的 _message_ids 列表，并持久化到 AgentState 中
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
            2. 构建 head：① AGENT（AGENT.md 系统提示）→ ② SKILLS 清单块（若装配
               SkillRegistry 且有可见 skill）→ ③ Memory.compile()（system/ 记忆块，
               若有）→ ④ INTENT 块（intent_enabled 且管线成功时）→ user_task。
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

        # head:① AGENT ② SKILLS ③ Memory ④ INTENT 块,每轮重建
        # 不进累积;末尾 user task。澄清早退时 head=[user 澄清文本] → 直接返回,
        # 不落盘不加载(澄清是"非任务轮",只走 CLI 层)。
        head = await self._build_head(task, force_dispatch=force_dispatch)
        if len(head) == 1 and head[0].role == "user":
            return head[0].content

        #: in-context 窗口每轮重建:跨轮回放统一经 MessageManager(SQL) 加载,避免: self._messages 跨 run 残留导致下一轮重复加载(每步都从权威源重新 load)。
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
            # (head 是本地列表,_refresh_head_memory 就地替换记忆消息)。
            self._refresh_head_memory(head)

            #: LLM 输入 = head 前段(AGENT/SKILLS/memory/INTENT) + in-context 回放历史 +
            #: 末尾当前 user task(恒末位——否则 LLM 会把回放历史里的旧任务误当当前任务)。
            messages = list(head[:-1]) + self._messages + [head[-1]]

            # 压缩检查:只改 in-context 窗口(驱逐旧对话 + 插摘要),SQL 原始消息由 MessageManager 保留(Recall 可追溯)。
            # structured 未注入(摘要生成器缺失) 时压缩不触发——避免 None.extract 崩溃,CLI 接线后恢复。
            if self._needs_compaction(messages) and self.structured is not None:
                # 截断续写与压缩重建互斥:压缩可能驱逐"半截+续写提示"(in-context 重建),先弃掉累积器里的半截——续写无参照即完整重答,避免「半截 + 完整重答」重复交付。
                accumulated.clear()
                from paperflow.core.memory.compaction import run_compaction
                new_window = await run_compaction(
                    self._messages, self.compaction, self.llm, self.structured)
                # 摘要落盘 + message_ids 更新为「摘要 + 保留尾部」——压缩产物跨轮
                # 持久,下轮 _load_in_context 按 message_ids 回放摘要、不回放被驱逐
                # 旧消息(SQL 原始消息仍全量保留,Recall 完整)。
                self._persist_compacted_window(new_window)

                # 更新内存中的窗口的 _messages 列表
                self._messages = new_window

                # head[:-1]：① AGENT（AGENT.md 系统提示）② SKILLS 清单块（若有可见 skill）③ Memory Blocks（核心记忆块，如 persona/human）④ INTENT Block（意图识别结果，若启用）
                # self._messages：从 MessageManager（SQL 持久化层）加载的该会话历史消息，加上本轮已产生的 assistant/tool 交互消息
                # head[-1]：当前的 user task 消息
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
                    self._append_to_messages(response)
                    self._append_to_messages(Message(
                        role="user",
                        content="上一条回答因输出长度上限被截断，请直接从断点继续输出，不要重复已输出的内容。"))
                    self._persist_conversation([response])
                    continue

                # 正常完成（未被截断）
                content = "".join(accumulated) + (response.content or "")
                accumulated.clear()

                # 执行安全中间件（SecurityScanMiddleware）的 on_finish 钩子（按顺序），
                # 每个中间件可以改写最终回答（如追加来源引用、注入安全声明等）
                for mw in self.security_middleware:
                    content = await mw.on_finish(self, content)

                # 如果启用了意图识别功能，并且本轮产生了意图（last_intent 非空） → 更新会话状态：记录本轮意图类型和用户输入，供下一轮追问或上下文理解使用。
                if self.intent_enabled and self.last_intent is not None:
                    self.conversation.prev_intent = self.last_intent.intent_type
                    self.conversation.prev_user_input = task
                # 最终回答(经 on_finish 改写——回放给下轮的是"用户看到的事实",
                # SAFE_PROMPT 等安全声明跨轮保留)落盘 + 进 in-context,供下轮回放
                final = Message(role="assistant", content=content)
                self._append_to_messages(final)
                self._persist_conversation([final])
                return content

            # LLM 请求调用工具：将 assistant 消息（含 tool_calls）加入 in-context
            # 并持久化到数据库，以便后续轮次（或跨 run）能回放该条消息。
            self._append_to_messages(response)
            self._persist_conversation([response])

            # 并发执行 LLM 请求的所有工具调用:同一 message 的多个工具调用用 gather 并行
            # 1. 使用 asyncio.gather 并行执行同一 message 中的多个工具调用，提升效率。
            # 2. 每个工具调用通过 _exec_tool 处理，内部包含中间件管道、参数解析、执行等。
            # 3. 并行上限使用信号量限制，防止一次性打爆外部 API 或数据库连接池。
            # 4. 确认回调（如高风险操作需用户确认）通过一个共享锁串行化，避免多个工具同时抢占 CLI 标准输入导致竞态。
            sem = asyncio.Semaphore(4)
            confirm_lock = asyncio.Lock()

            # 内部协程：每个工具调用受信号量限制，并传入确认锁和当前轮次。
            async def _run_one(tc: dict) -> ToolResult:
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
                    Message(role="tool", content=_CANCELLED_TOOL_MSG,
                            tool_call_id=tc["id"])
                    for tc in response.tool_calls
                ]
                self._append_to_messages(synth)
                self._persist_conversation(synth)
                raise

            # 将工具执行结果以 tool 角色消息加入 in-context，并持久化
            # tool_call_id 字段将结果与对应的 tool_call 请求关联，LLM 在下一轮推理时能看到每个调用的返回值。
            for tc, result in zip(response.tool_calls, results):
                tool_msg = Message(
                    role="tool",
                    content=result.text,
                    tool_call_id=tc["id"],
                )
                self._append_to_messages(tool_msg)
                self._persist_conversation([tool_msg])

            # 终止型工具：submit 类成功提交即本 agent 任务终结——直接
            # 结束 ReAct 循环，不再进下一轮 LLM 调用（实测 reviewer 曾重复提交 95 次，
            # 每轮 ~1.5 万 tokens）。返回值即提交文本：spawn 的 digest 提取依赖裁决
            # 全文，不能只回一句「已提交」。on_finish 钩子照常执行（安全扫描一致性）。
            if any(r.summary.get("terminal") for r in results):
                final_text = next(r.text for r in results if r.summary.get("terminal"))
                for mw in self.security_middleware:
                    final_text = await mw.on_finish(self, final_text)
                final = Message(role="assistant", content=final_text)
                self._append_to_messages(final)
                self._persist_conversation([final])
                return final_text

            # 本轮工具调用处理完毕，循环继续（回到开头，将新的上下文送交 LLM 进行下一轮推理）。

        # 安全阀触发：LLM 陷入了无法在限定轮数内退出的循环。
        # 失败残渣随消息逐条落盘（Recall 保留原始记录含失败轮）；MaxTurnsExceeded
        # 只中断本轮回放，不回滚已落盘消息。下轮 _load_in_context 会看到失败残渣，
        # 由调用方（CLI）决定是否重试。
        raise MaxTurnsExceeded(
            f"ReAct loop did not finish within {self.max_turns} turns"
        )

    def _make_llm_telemetry(self, turn: int):
        """构造 LLM 调用 telemetry 回调（sync，可能跑在线程池线程）。

        每次调用传独立回调（而非共享属性）——同一轮多个 spawn 调用下多个子 agent 共享
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

        # 0. 发送工具调用事件（流式渲染）
        # 目的：在参数解析前就发出流式事件，这样即使后续出现 JSON 解析失败或未知工具，终端渲染器也能及时清空中间内容缓冲区，避免将思考文本误判为最终答案。
        # 门控：仅当 stream_callback 存在时才执行（即 CLI 交互模式），否则零开销。
        if self.stream_callback is not None:
            self._emit(StreamEvent("tool", _format_tool_call(
                name, tool_call["function"]["arguments"]), self.agent_type))

        # 1. 按工具名查找 Tool 实例
        # 若 self.tools 中无此名称，说明 LLM 幻觉或受提示注入攻击生成了非法工具名。此时 tool = None，后续会处理并返回错误 ToolResult。
        tool = self.tools.get(name)

        # 2. 构建 ToolContext（工具调用上下文对象）
        # 提前构造 ctx 是设计关键：即使参数解析失败或工具不存在，也要能够执行 after 钩子（审计、日志等），保证所有执行路径都有审计记录。
        # ctx 会贯穿整个管道，中间件可在 before/after 中读写其属性（如添加额外元数据、修改结果等）。
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

        # 3. 解析 JSON 参数
        # LLM 生成的 arguments 是 JSON 字符串，必须解析为 dict。
        # 若解析失败（非法 JSON），记录错误到 ctx，执行 after 钩子，然后返回带有解析错误的 ToolResult，让 LLM 自己决定是否重试。
        try:
            raw_args = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError as e:
            # LLM 生成了非法 JSON → 走 after 链记录审计后反馈给 LLM
            ctx.error = e
            await self._run_after_hooks(ctx)
            return ToolResult(text=f"Tool argument parse error: {e}")

        # 4. 参数归一化
        # 如果 raw_args 不是 dict（例如 LLM 生成了数组或字符串），为了安全将其归一化为空字典 {}，防止后续 tool.execute(**ctx.args) 时展开崩溃。
        # 这种异常情况也会被记录在 ctx.args 中供审计。
        ctx.args = raw_args if isinstance(raw_args, dict) else {}

        # 同路径写/编辑串行化（见 _path_locks 说明）：仅对需确认的写类工具生效，
        # 锁覆盖「确认决策 + 执行」全程；其余工具零开销直通。
        if getattr(tool, "requires_confirm", False) and isinstance(ctx.args.get("path"), str):
            async with _path_lock(ctx.args["path"]):
                return await self._exec_tool_guarded(tool, ctx, _confirm_lock, turn)
        return await self._exec_tool_guarded(tool, ctx, _confirm_lock, turn)

    async def _exec_tool_guarded(self, tool, ctx, _confirm_lock, turn) -> ToolResult:
        """确认与执行段（_exec_tool 的 5-8 步）：同路径锁保护下运行。"""

        # 5. 处理未知工具（LLM 幻觉或 prompt injection）
        # 若工具不存在（tool is None），记录错误，执行 after 钩子，并返回可用工具列表，帮助 LLM 纠正。
        # 注意：此时不会执行 before 钩子（因为无工具可执行），但 after 钩子仍会运行，确保审计覆盖。
        if tool is None:
            ctx.error = ValueError(f"Unknown tool: {ctx.tool_name}")
            await self._run_after_hooks(ctx)
            return ToolResult(
                text=f"Unknown tool: {ctx.tool_name}. Available: {list(self.tools.keys())}"
            )

        # 6. before 阶段：顺序执行所有安全中间件的 before 钩子。
        result = await self._run_before_hooks(ctx, _confirm_lock)
        if result is not None:
            # 若 before 阶段返回了 ToolResult（拒绝/拦截），直接返回给 LLM
            return result

        # 7. 执行工具逻辑（结果统一规范化为 ToolResult）
        # 经过 before 阶段后，确认工具可以执行。
        # 使用 asyncio.to_thread 将工具放到线程池执行，避免阻塞事件循环。
        # 对于 CPU/网络密集型操作，这能保证并发调度不被单个长耗时任务阻塞。
        #
        # 特殊处理：若工具声明了 wants_run_state=True（搜索类工具），则注入一个按 trace_id 键控的去重池（_run_state），用于跨调用共享已访问的 URL 或文件，避免重复抓取。
        # 该注入通过额外参数 _run_state 传递，不写入 ctx.args，因为 ctx.args 会被序列化用于审计，而去重池不可序列化。
        try:
            if getattr(tool, "async_execute", False):
                # 异步工具（如 spawn）：在当前事件循环上直接 await——子 agent 与父
                # 同循环，父被取消（Ctrl+C）时 CancelledError 沿 await 链级联传播，
                # 整棵 agent 树一起终止（P0-2 根治），不再经 to_thread 留孤儿线程。
                raw = await tool.aexecute(**ctx.args)
            elif getattr(tool, "wants_run_state", False):
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
        # 完成摘要（写/编辑工具）经 tool 事件发到渲染器——用户看到 File written/edited
        # 完成行；门控 stream_callback（非 CLI 调用方零开销）。复用 "tool" kind 无需新 kind。
        if ctx.result.completion and self.stream_callback is not None:
            self._emit(StreamEvent("tool", ctx.result.completion, self.agent_type))
        return ctx.result

    async def _run_before_hooks(self, ctx: ToolContext, confirm_lock: asyncio.Lock | None = None) -> ToolResult | None:
        """
        顺序执行所有安全中间件的 before 钩子。
        每个中间件可以：
          - 正常返回：放行，继续下一个中间件
          - 抛出 ConfirmRequired：需要用户确认（高风险操作）
          - 抛出 SecurityError（或其子类）：策略拦截（如拒绝访问敏感文件）

        若所有钩子通过，返回 None，表示可以继续执行工具。
        若中途拦截（用户拒绝、策略阻止），则返回一个 ToolResult，调用方应直接返回该结果，不再执行工具。

        内部处理：
            - 捕获 ConfirmRequired → 调用确认回调（串行化），记录审批事件
            - 捕获 SecurityError → 直接返回策略拒绝的 ToolResult
        """
        for mw in self.security_middleware:
            try:
                await mw.before(ctx)
            except ConfirmRequired as cr:
                # ----- 需要用户确认 -----
                # 先通知中间件“请求已发出”
                for mw in self.security_middleware:
                    await mw.on_approval(ctx, "requested")

                # 定义异步决策函数，调用外部确认回调
                async def _decide() -> bool:
                    return await self.confirm_callback(cr)

                # 串行化确认（若提供了锁）
                try:
                    if confirm_lock is not None:
                        async with confirm_lock:
                            confirmed = await _decide()
                    else:
                        confirmed = await _decide()
                except asyncio.CancelledError:
                    # 取消路径审计闭环（P0-3）：approval_requested 已发出但决策
                    # 未落——显式结算为 cancelled 后再抛，审计不再出现「有 requested
                    # 无 decided」的悬空；确认集合不加键（下次同路径仍会询问）。
                    for mw in self.security_middleware:
                        await mw.on_approval(ctx, "decided", approval_outcome="cancelled")
                    raise

                # 根据确认结果和是否有人工回调，标记决策类型
                # 决策语义:确认通过 → user_confirmed;被拒时按是否有真实人工回调区分
                # user_denied / auto_denied(fail-safe 默认拒绝,无人值守)。
                outcome = (
                    "user_confirmed" if confirmed else (
                        "user_denied" if self._has_human_confirm else "auto_denied"))
                ctx.approval_outcome = outcome

                # 通知中间件决策已做出
                for mw in self.security_middleware:
                    await mw.on_approval(ctx, "decided", approval_outcome=outcome)

                if not confirmed:
                    # 拒绝 → 记录错误并走 after 钩子，反馈给 LLM
                    # 注意：此时工具尚未执行，但 after 钩子仍会运行以记录审计。
                    # summary.decision 用已计算的 outcome（user_denied/auto_denied），
                    # 与 ctx.approval_outcome 保持一致——拒绝路径带决策依据回放给 LLM
                    ctx.error = cr
                    await self._run_after_hooks(ctx)
                    return ToolResult(
                        text=f"User denied: {cr.tool_name}",
                        summary={"decision": outcome, "tool": cr.tool_name},
                    )

                # 确认通过：标记 ctx，继续执行（不返回，后续会执行工具）
                cr.confirm()
                ctx.user_confirmed = True
            except SecurityError as se:
                # PolicyEngineMiddleware 抛出的 policy_denied /
                # SecurityScanMiddleware/WorkspacePolicyMiddleware 抛出的 security_blocked
                # → 带决策摘要返回
                ctx.error = se
                await self._run_after_hooks(ctx)
                return ToolResult(
                    text=f"{se.decision}: {se.reason}",
                    summary={"decision": se.decision, "violations": getattr(se, "violations", [])},
                )
        # 全部通过
        return None

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
