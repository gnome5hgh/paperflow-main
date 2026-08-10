"""共享 ask_user_question 工具——向用户提问并等待回答。

原属 supervisor 私有,子 agent(writer/qa-agent)接入中途问用户后上移共享层:
一处定义、多处装配。权限卡在装配面——searcher/reviewer 不装配即无权问。
"""
from paperflow.core.tool import Tool, ToolResult


class AskUserQuestionTool(Tool):
    """向用户提问并等待回答(阻塞当前 ReAct 轮)。

    经父 agent 注入的 ask_user_callback 读 stdin;callback 为空(程序化/测试环境)
    时返回"无法交互"提示,由调用 agent 基于已有信息自行决策,不挂死。
    """

    name = "ask_user_question"
    description = "向用户提问并等待回答（阻塞直到用户输入）。答案作为工具结果返回。"
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "要问用户的问题"}},
        "required": ["question"],
    }
    needs_parent = True
    risk_level = "low"

    def execute(self, question: str) -> ToolResult:
        cb = getattr(self._parent, "ask_user_callback", None)
        if cb is None:
            # fail-safe：无法交互时明确告知,调用 agent 依据已有信息自行决策(不挂死)
            return ToolResult(text="无法交互：当前环境未提供用户回调，请基于已有信息决定")
        # cb 由 CLI 注入,在 worker 线程里读 stdin(阻塞等待用户输入,不冻结事件循环)
        answer = cb(question)
        return ToolResult(text=f"用户回答：{answer}")
