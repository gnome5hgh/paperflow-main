# paperflow/tools/orchestration/ask_user.py
"""共享 ask_user_question 工具——向用户提问并等待回答。

原属 supervisor 私有,子 agent(note-agent/paper-agent/rag-agent)接入中途问用户后上移共享层:
一处定义、多处装配。权限卡在装配面——review-agent 不装配即无权问。

意图确认通道（澄清统一）：可选 intent_options 参数把「向用户确认意图」
变成代码级协议——工具展示编号选项、用 match_option_choice 解析回复、命中即更新
父 agent 的会话意图（last_intent + conversation.prev_intent），后续 spawn 门禁按
确认意图放行。若只确认不更新意图，spawn 门禁仍按旧意图拒绝——用户确认会困在
工具结果里，同一轮里反复被拦。
"""
from paperflow.core.intent.routing.confirm import format_intent_options, match_option_choice
from paperflow.core.intent.constants import IntentStep, IntentType
from paperflow.core.intent.schemas.intent import IntentOutput, IntentUnit
from paperflow.core.tool import Tool, ToolResult


class AskUserQuestionTool(Tool):
    """向用户提问并等待回答(阻塞当前 ReAct 轮)。

    经父 agent 注入的 ask_user_callback 读 stdin;callback 为空(程序化/测试环境)
    时返回"无法交互"提示,由调用 agent 基于已有信息自行决策,不挂死。

    Attributes:
        name: str，工具名 "ask_user_question"
        description: str，工具描述（含 intent_options 语义）
        parameters: dict，JSON Schema（question/intent_options）
        needs_parent: bool，True（经父 agent 的 ask_user_callback 读输入）
        risk_level: str，"low"
        _parent: Agent，父 agent 引用（提供 ask_user_callback 与会话意图）
    """

    name = "ask_user_question"
    description = (
        "向用户提问并等待回答（阻塞直到用户输入）。答案作为工具结果返回。"
        "需要用户在意图间取舍时必须带 intent_options 提供候选意图（用户的选择会"
        "代码级更新会话意图，后续派发按确认意图放行）；仅询问补充信息时省略该参数。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问用户的问题"},
            "intent_options": {
                "type": "array",
                "items": {"type": "string", "enum": [t.value for t in IntentType]},
                "maxItems": 3,
                "description": (
                    "候选意图枚举值（2-3 个，按展示顺序）。仅在需要用户确认意图时提供；"
                    "提供后问题会自动追加编号选项，用户回复经代码解析并落地会话意图。"
                ),
            },
        },
        "required": ["question"],
    }
    needs_parent = True
    risk_level = "low"

    def execute(self, question: str, intent_options: list[str] | None = None) -> ToolResult:
        """向用户提问并返回其回答;无回调时返回 fail-safe 提示,不阻塞挂死。

        callback 为空(程序化/测试环境)时明示无法交互,由调用 agent 基于已有信息
        自行决策;有回调时经 worker 线程读 stdin,不冻结事件循环。
        带 intent_options 时：代码追加编号选项行，回复可解析为候选之一则更新
        父 agent 的会话意图并在结果中明示；解析不出则如实告知模型意图未变。

        Args:
            question: str，要问用户的问题
            intent_options: list[str] | None，候选意图枚举值（2-3 个）

        Returns:
            ToolResult，文本为「用户回答：…」；空回答返回明确提示，意图选项命中则注明会话意图已更新。
        """
        cb = getattr(self._parent, "ask_user_callback", None)
        if cb is None:
            # fail-safe：无法交互时明确告知,调用 agent 依据已有信息自行决策(不挂死)
            return ToolResult(text="无法交互：当前环境未提供用户回调，请基于已有信息决定")
        options: list[IntentType] = []
        display = question
        if intent_options:
            for v in intent_options[:3]:
                try:
                    t = IntentType(v)
                except ValueError:
                    continue
                if t not in options:
                    options.append(t)
            if options:
                display = f"{question}\n{format_intent_options(options)}"
        # cb 由 CLI 注入,在 worker 线程里读 stdin(阻塞等待用户输入,不冻结事件循环)
        answer = cb(display)
        if not answer.strip():
            # 裸空串会诱发模型脑补（把空回答编造成「任务被外部打断」）
            return ToolResult(text="用户回答：（空/超时/中断，未给出回答——请基于已有信息自行决策，勿推测用户另有指示）")
        if options:
            confirmed = match_option_choice(answer, options)
            if confirmed is not None:
                self._apply_confirmed_intent(confirmed)
                return ToolResult(
                    text=(f"用户回答：{answer}\n"
                          f"（已确认意图：{confirmed.value}——会话意图已在代码层更新，"
                          f"后续 spawn 按该意图放行，无需再向用户确认意图）"))
            return ToolResult(
                text=(f"用户回答：{answer}\n"
                      "（未识别为候选意图的选择，会话意图未变；如仍需确认意图，"
                      "请让用户直接回复选项编号）"))
        return ToolResult(text=f"用户回答：{answer}")

    def _apply_confirmed_intent(self, confirmed: IntentType) -> None:
        """用户确认意图的代码级落地：父 agent 的 last_intent 立即更新（spawn 门禁
        本轮即按确认意图放行），conversation.prev_intent 同步（下一轮追问检测继承
        正确意图；run() 收尾的本轮覆写读到的是同一个确认值，状态自洽）。

        合成 IntentOutput 仅填门禁/审计所需字段——INTENT 块在 run 开始时已构建，
        mid-run 更新不影响本轮注入的上下文。

        Args:
            confirmed: IntentType，用户确认的意图

        Returns:
            无返回值；就地更新父 agent 的 last_intent 与 conversation.prev_intent。
        """
        parent = self._parent
        parent.last_intent = IntentOutput(
            intents=[IntentUnit(intent_type=confirmed, confidence=1.0)],
            source=IntentStep.USER)
        conversation = getattr(parent, "conversation", None)
        if conversation is not None:
            conversation.prev_intent = confirmed
