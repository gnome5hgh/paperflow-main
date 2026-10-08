"""remove_citation：从 references.bib 删除一条引用（不可逆，需用户确认）。"""
from paperflow.core.tool import Tool, ToolResult


class RemoveCitationTool(Tool):
    """从 references.bib 删除一条引用的工具（需用户确认）。

    Attributes:
        name: str，工具名 "remove_citation"
        description: str，工具描述
        parameters: dict，JSON Schema（key_or_title）
        risk_level: str，"medium"（写类形态，删除不可逆但影响面有限）
        requires_confirm: bool，True（经策略引擎第 3 级询问用户）
        side_effects: list[str]，["delete_file"]
        manager: CitationManager，注入的引用库门面
    """
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
        """注入引用库门面。

        Args:
            manager: CitationManager，引用库读写入口
        """
        self.manager = manager

    def effective_target_path(self, args: dict) -> str | None:
        """导出写互斥键：本工具从引用库删条目，落点就是那个 bib 文件。

        本工具的参数是 bib key 或标题，没有 path 参数；真正被改的是 references.bib。
        经它键控后，两路并发改同一个 bib 会当场被拒，而不是被底层写入锁静默串行——
        并发因此是可见的，而不是悄悄发生。

        Args:
            args: dict，已解析的工具调用参数（未使用，仅为签名与基类一致）

        Returns:
            references.bib 的绝对路径。
        """
        return str(self.manager.bib_path)

    def execute(self, key_or_title: str) -> ToolResult:
        """按 key 或标题删除；多条命中时返回候选交用户选择。

        Args:
            key_or_title: str，bib key 或论文标题

        Returns:
            ToolResult，文本为删除结果/候选列表/未找到提示。
        """
        r = self.manager.remove(key_or_title)
        if r["status"] == "ambiguous":
            lines = "\n".join(f"- {c['key']}: {c['title']}" for c in r["candidates"])
            return ToolResult(text=f"多个候选，请让用户选择后再删：\n{lines}")
        if r["status"] == "not_found":
            return ToolResult(text=f"未找到「{key_or_title}」对应的条目")
        text = f"已删除 {r['key']}\nbib: {self.manager.bib_path}"
        return ToolResult(text=text, summary={"status": r["status"], "key": r["key"]})
