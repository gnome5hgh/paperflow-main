"""remove_citation：从 references.bib 删除一条引用（不可逆，需用户确认）。"""
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
    # 删除不可逆，但影响面限于库内单条原文块、可重加；对齐 write_file/edit_file
    # 的写类形态——medium 过默认阈值（config.runtime.max_risk="medium"），
    # 再经 requires_confirm 在策略引擎第 3 级询问用户，而不是在第 2 级被
    # 风险阈值直接拒（high > medium）。
    risk_level = "medium"
    requires_confirm = True
    side_effects = ["delete_file"]

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
