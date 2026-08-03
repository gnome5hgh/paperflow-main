"""FormatAnswerTool：格式化最终回答输出。

内容安全扫描（format="content" → SecurityScan critical 硬阻断）由中间件执行，
工具自身只做格式化。
"""
from paperflow.core.tool import Tool, ToolResult


class FormatAnswerTool(Tool):
    name = "format_answer"
    description = "格式化最终回答输出（内容安全扫描硬阻断由中间件执行）"
    parameters = {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "format": "content", "description": "回答文本"},
        },
        "required": ["answer"],
    }
    risk_level = "low"

    def execute(self, answer: str) -> ToolResult:
        return ToolResult(text=f"## 回答\n\n{answer}")
