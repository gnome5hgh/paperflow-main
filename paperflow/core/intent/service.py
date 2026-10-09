# paperflow/core/intent/service.py
"""意图识别的集成适配器——把「进 ReAct 之前」的全部意图逻辑收在一处。

意图识别是可选的预处理层：这里独占管线调用、INTENT 块与规则块的渲染、同步
澄清、收尾账本、跨轮状态回写，并持有会话状态。Agent 的 ReAct 循环只持一个
可选的本类实例、在固定钩子点调用；引用为 None 即「关」，一切退化为空操作。
"""
import asyncio
import logging
from dataclasses import dataclass, field

from paperflow.core.intent.constants import INTENT_LABELS_ZH, IntentStep, IntentType
from paperflow.core.intent.conversation_state import ConversationState
from paperflow.core.intent.routing.confirm import format_intent_options, match_option_choice
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
        last_intent: IntentOutput | None，本轮意图产出（供账本与回写）
    """

    #: 意图规则块：随 INTENT 块注入的字段语义与「非派发意图」处理说明。
    #: 由意图层产出（而非写死在 supervisor 的 AGENT.md），关掉意图识别时不出现。
    rules_block: str = ""

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
        #: 本轮识别出的完整意图列表（收尾核对的事实来源，非派发队列）。
        self._recognized_steps: list[IntentType] = []
        #: 本轮是否已注入过收尾核对（每个 run 至多一次）。
        self._steps_checked: bool = False

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
            self._recognized_steps = []
            self._steps_checked = False
            return Turn(head_block=None, task=task, intents=[])

        if intent.clarification:
            intent, task = await self._resolve_clarification(task, intent)
        self.last_intent = intent
        self._recognized_steps = [u.intent_type for u in intent.intents]
        self._steps_checked = False
        block = "INTENT: " + intent.model_dump_json(
            exclude={"clarification", "prev_intent", "clarify_candidates"})
        return Turn(head_block=block, task=task, intents=list(self._recognized_steps))

    def needs_ledger(self) -> bool:
        """是否需要注入收尾核对账本（识别到 ≥2 个意图且本轮尚未核对过）。

        Returns:
            True 表示本轮需注入收尾核对账本。
        """
        return len(self._recognized_steps) >= 2 and not self._steps_checked

    def mark_ledger_injected(self) -> None:
        """标记本轮已注入收尾核对（防重复注入）。

        Returns:
            无返回值。
        """
        self._steps_checked = True

    def render_ledger(self, dispatches, artifacts) -> str:
        """渲染收尾核对消息（只摆事实、不下结论）。

        Args:
            dispatches: list[tuple[str, str]]，本轮派发账本（agent_type, status）
            artifacts: list[str]，本轮新落盘的产物路径

        Returns:
            收尾核对消息文本（识别到的意图 + 派发记录 + 产物清单）。
        """
        steps = "、".join(INTENT_LABELS_ZH.get(t, t.value) for t in self._recognized_steps)
        dispatched = "、".join(f"{a}({s})" for a, s in dispatches) or "无"
        produced = "、".join(artifacts) or "无"
        return ("（系统核对）本轮识别出的意图：{steps}。\n"
                "本轮已派发的子任务记录：{dispatched}。\n"
                "本轮新落盘的产物：{produced}。\n"
                "请核对：若有意图未派发、或某次派发失败/超时/被拒，必须在最终回答中如实说明；"
                "全部完成则正常汇报，不要提及本条提示。").format(
            steps=steps, dispatched=dispatched, produced=produced)

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

    def record_confirmed(self, intent_name: str) -> None:
        """落地用户确认的意图（供 ask_user 的 intent_options 路径调用）。

        Args:
            intent_name: str，确认的意图枚举值

        Returns:
            无返回值；就地更新 last_intent 与 conversation.prev_intent。
        """
        confirmed = IntentType(intent_name)
        self.last_intent = IntentOutput(
            intents=[IntentUnit(intent_type=confirmed, confidence=1.0)],
            source=IntentStep.USER)
        self.conversation.prev_intent = confirmed

    def format_options(self, names: list[str]) -> str:
        """把候选意图名格式化为带编号的选项行（供 ask_user 展示）。

        Args:
            names: list[str]，候选意图枚举值

        Returns:
            编号选项行文本。
        """
        options = [IntentType(n) for n in names if n in {t.value for t in IntentType}]
        return format_intent_options(options)

    def parse_choice(self, answer: str, names: list[str]) -> str | None:
        """把用户回复解析为候选之一（供 ask_user 落地）。

        Args:
            answer: str，用户回复原文
            names: list[str]，候选意图枚举值

        Returns:
            命中的候选枚举值；解析不出返回 None。
        """
        options = [IntentType(n) for n in names if n in {t.value for t in IntentType}]
        confirmed = match_option_choice(answer, options)
        return confirmed.value if confirmed is not None else None

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
