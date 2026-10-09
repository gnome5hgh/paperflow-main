# paperflow/core/intent/service.py
"""意图识别的集成适配器——把「进 ReAct 之前」的全部意图逻辑收在一处。

意图识别是可选的预处理层：这里独占管线调用、INTENT 块与规则块的渲染、同步
澄清、跨轮状态回写，并持有会话状态。Agent 的 ReAct 循环只持一个
可选的本类实例、在固定钩子点调用；引用为 None 即「关」，一切退化为空操作。
"""
import asyncio
import logging
from dataclasses import dataclass, field

from paperflow.core.intent.constants import IntentStep, IntentType
from paperflow.core.intent.conversation_state import ConversationState
from paperflow.core.intent.routing.confirm import match_option_choice
from paperflow.core.intent.routing.entities import extract_entities
from paperflow.core.intent.schemas.intent import IntentOutput, IntentUnit

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """一次 run 开头的意图预处理结果。

    Attributes:
        head_block: str | None，要注入的 INTENT 块文本；无意图（降级）时为 None
        task: str，可供使用的任务文本（澄清答案命中时带附录）
        intents: list[IntentType]，本轮识别到的意图列表（首项即主意图）
    """
    head_block: str | None
    task: str
    intents: list[IntentType] = field(default_factory=list)


class IntentService:
    """意图识别集成适配器（Agent 与意图模块之间的唯一缝）。

    Attributes:
        pipeline: IntentPipeline，五级级联管线
        conversation: ConversationState，跨轮状态（prev_intent / prev_user_input）
        ask_user_callback: Callable[[str], str] | None，同步澄清的问询回调
        last_intent: IntentOutput | None，本轮意图产出（供跨轮回写）
    """

    #: 意图规则块：随 INTENT 块注入的字段语义与「非派发意图」处理说明。
    #: 由意图层产出（而非写死在 supervisor 的 AGENT.md），关掉意图识别时不出现。
    rules_block: str = (
        "## 意图层（可选预处理）\n\n"
        "INTENT 块是框架意图识别的输出(意图列表/实体/改写后的 query/来源),"
        "是**强提示,不是命令**——它不决定你派谁、以什么顺序派;选型与编排按 "
        "`<available_agents>` 清单与你的判断。你可在边界内自主判断:合并相邻请求、"
        "追问后再派发、选择更合适的子任务拼装方式。\n\n"
        "### 这些意图各有固定动作(无需按能力选型)\n\n"
        "| 意图 | 类别 | 你的动作 |\n"
        "|------|------|---------|\n"
        "| `menu_selection` | 对话管理 | 用户在回复你上一轮给出的编号菜单。对照你上轮菜单内容，把所选选项转成对应动作/派发（如选项是「科研发现」→ 派 research-agent 并拼入课题）；菜单已过时或无法对应选项 → 先向用户问清（把问题写进你的回答），不猜 |\n"
        "| `record_user_info` | 业务 | 用户陈述自己的信息(研究方向/专业/偏好)：派 memory-agent 写进核心块,再由你在回答里引导下一步;方向过宽(如\"课题是AI\")→ 先追问细分。**不派发领域 agent**(没有领域工作要做) |\n"
        "| `manage_memory` | 业务 | 查询(读过哪些/未读清单)、加入未读、移出未读等记忆与清单操作:派 memory-agent 执行,子任务写明具体动作与标题或路径 |\n"
        "| `chitchat` | 系统 | 轻量回复 + 温和引导回学术场景。不派发 |\n"
        "| `out_of_scope` | 系统 | 明确拒绝 + 说明能力边界(代写论文属学术不端,必须拦截)。不派发 |\n"
        "| `help` | 系统 | 返回功能卡片/示例 Query 列表。不派发 |\n"
        "| `feedback` | 系统 | 派 memory-agent 把反馈写入日志块；本意图只放行 memory-agent,不派发领域 agent |\n\n"
        "### 字段语义\n\n"
        "| 情形 | 语义 |\n"
        "|------|------|\n"
        "| `source=user` | 用户已确认的意图（澄清编号选择），代码级落地,直接按该意图调度;不要怀疑或再次向用户确认意图 |\n"
        "| `entities` | pdf_path / arxiv_id / doi / note_path / figure 已提取,直接拼进子任务文本(不要重新解析) |\n\n"
        "### INTENT 块字段各自的作用\n\n"
        "- `intents` — 意图列表,**首项即主意图**:对请求性质的判断,说明用户在做什么。"
        "它**不只是选型依据**——非派发意图的动作见上表,选型则按 `<available_agents>` 的能力。\n"
        "- `intents` 长度 >1 即复合请求信号,供你规划;**顺序与并行你自己定**,框架不强制。\n"
        "- `source` — 意图的可信度来源:`source=user` 是用户已确认,直接照做,不要重复确认。\n"
        "- `entities` — 已抽取的 pdf_path / arxiv_id / doi / note_path / figure,直接拼进子任务文本"
        "(不要重新解析)。**追问轮**(「再找近五年的」)里它已合并上轮实体(上轮在前、同键被本轮覆盖),"
        "继承的上轮约束看这里。\n"
        "- `rewritten_query` — 当前消息(可能被 LLM 改写)的文本,可作检索 query 的起点;**不含**上轮信息。"
    )

    def __init__(self, pipeline, conversation: ConversationState,
                 ask_user_callback=None):
        """绑定管线、会话与问询回调。

        Args:
            pipeline: IntentPipeline，含路由与 LLM 兜底的完整管线
            conversation: ConversationState，跨轮意图状态
            ask_user_callback: Callable[[str], str] | None，澄清问询回调（None 时放弃澄清）
        """
        self.pipeline = pipeline
        self.conversation = conversation
        self.ask_user_callback = ask_user_callback
        self.last_intent: IntentOutput | None = None

    async def begin(self, task: str) -> Turn:
        """跑管线、同步澄清，产出要注入的块与最终任务文本。

        管线失败降级为空（不阻断主流程、不更新跨轮意图）。

        Args:
            task: str，本轮原始任务文本

        Returns:
            Turn：head_block 为 INTENT 块（无意图时为 None），task 为澄清后可用文本，
            intents 为本轮识别到的意图列表。
        """
        try:
            intent = await self.pipeline.run(
                task, prev_intent=self.conversation.prev_intent,
                prev_user_input=self.conversation.prev_user_input)
        except Exception:
            logger.warning("intent pipeline failed, degraded to plain ReAct", exc_info=True)
            self.last_intent = None
            return Turn(head_block=None, task=task, intents=[])

        if intent.clarification:
            intent, task = await self._resolve_clarification(task, intent)
        self.last_intent = intent
        block = "INTENT: " + intent.model_dump_json(
            exclude={"clarification", "prev_intent", "clarify_candidates"})
        return Turn(head_block=block, task=task,
                    intents=[u.intent_type for u in intent.intents])

    def finish(self, task: str) -> None:
        """run 收尾回写跨轮状态：单意图轮记 prev_intent，多意图轮置 None。

        Args:
            task: str，本轮任务文本（写入 prev_user_input）

        Returns:
            无返回值。
        """
        if self.last_intent is None:
            return
        intents = self.last_intent.intents
        self.conversation.prev_intent = (
            intents[0].intent_type if len(intents) == 1 else None)
        self.conversation.prev_user_input = task

    async def _resolve_clarification(self, task: str, intent) -> tuple:
        """同步澄清：把管线的澄清问题问出去，答案在代码层落地为意图。

        无回调（程序化环境）时放弃澄清、按最佳猜测继续（fail-safe），绝不挂起。

        Args:
            task: str，原始任务文本
            intent: IntentOutput，含 clarification 的意图产出

        Returns:
            (最终意图, 最终任务文本)；无回调时放弃澄清按最佳猜测继续。
        """
        cb = self.ask_user_callback
        question = intent.clarification
        if cb is None:
            intent.clarification = None
            return intent, task
        answer = await asyncio.to_thread(cb, question)
        candidates = intent.clarify_candidates or []
        confirmed = match_option_choice(answer, candidates) if answer.strip() else None
        if confirmed is not None:
            resolved = IntentOutput(
                intents=[IntentUnit(intent_type=confirmed, confidence=1.0)],
                entities=extract_entities(task), rewritten_query=task,
                source=IntentStep.USER,
                prev_intent=self.conversation.prev_intent)
            resolved.clarification = None
            return resolved, f"{task}（用户澄清：{answer}）"
        if answer.strip():
            task = f"{task}（用户澄清：{answer}）"
        intent.clarification = None
        return intent, task
