"""SuggestEditTool：汇总对一篇笔记的修改建议（review-note 返回）。

execute 不读文件内容（只把 suggestions 按 path 标签格式化），放开 scratch 根零安全影响。
"""
from paperflow.core.tool import Tool, ToolResult


class SuggestEditTool(Tool):
    name = "suggest_edit"
    description = "汇总对一篇笔记的修改建议（供 review-note 返回）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "笔记绝对路径"},
            "suggestions": {"type": "array", "items": {"type": "string"},
                            "description": "修改建议列表"},
        },
        "required": ["path", "suggestions"],
    }
    risk_level = "low"
    # 审稿流目标是 scratch 草稿路径（review-note 对草稿给建议），与 FormatCheckTool 同根；
    # 不加 scratch 时真实 WorkspacePolicy 会拦截草稿路径（draft 在 workspace/tmp），
    # 且 execute 不读文件内容（只把 suggestions 按 path 标签格式化），放开零安全影响。
    allowed_roots = ["note", "scratch"]

    def execute(self, path: str, suggestions: list[str]) -> ToolResult:
        lines = "\n".join(f"- {s}" for s in suggestions)
        return ToolResult(text=f"对 {path} 的建议：\n{lines}")
