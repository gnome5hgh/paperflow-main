"""remove_citation：从 references.bib 删除一条引用（高危，需用户确认）。"""
from paperflow.core.tool import Tool, ToolResult


class RemoveCitationTool(Tool):
    name = "remove_citation"
    description = ("从 references.bib 删除一条引用（传 bib key 或论文标题）。"
                   "多条标题命中时返回候选列表——必须先让用户选择，不得擅自删。")
    parameters = {
        "type": "object",
        "properties": {
            "key_or_title": {"type": "string", "description": "bib key 或论文标题"},
        },
        "required": ["key_or_title"],
    }
    risk_level = "high"                 # 不可逆删除，走写类确认门禁
    side_effects = ["write_file"]

    def __init__(self, manager):
        self.manager = manager

    def execute(self, key_or_title: str) -> ToolResult:
        r = self.manager.remove(key_or_title)
        if r["status"] == "ambiguous":
            lines = "\n".join(f"- {c['key']}: {c['title']}" for c in r["candidates"])
            return ToolResult(text=f"多个候选，请让用户选择后再删：\n{lines}")
        if r["status"] == "not_found":
            return ToolResult(text=f"未找到「{key_or_title}」对应的条目")
        text = f"已删除 {r['key']}\nbib: {self.manager.bib_path}"
        return ToolResult(text=text, summary={"status": r["status"], "key": r["key"]})
