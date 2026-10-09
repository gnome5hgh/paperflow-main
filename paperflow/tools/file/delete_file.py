"""DeleteFileTool：删除一个语料文件（不可逆，需用户确认）。

比 write_file 更保守：只收精确的绝对路径，不做便捷入口、不做 basename 容错搜索。
写错地方只是写错，删错文件不可逆，所以宁可让调用方多给一次完整路径。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult


class DeleteFileTool(Tool):
    """删除单个文件的写入类工具（需用户确认）。

    Attributes:
        name: str，工具名 "delete_file"
        description: str，工具描述
        parameters: dict，JSON Schema（path）
        risk_level: str，"medium"（删除不可逆但影响面限于单个文件）
        requires_confirm: bool，True（经策略引擎第 3 级询问用户）
        side_effects: list[str]，["delete_file"]（据此纳入同路径写互斥）
    """

    name = "delete_file"
    description = (
        "删除一个文件（不可逆）。path 必须是精确的绝对路径，"
        "不支持通配符、不支持批量、不猜测相近文件名；目录一律拒绝。"
        "删掉的是论文 PDF 时，索引里残留的条目需要再跑一次索引收敛才会被清掉。")
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "format": "path",
                "description": "要删除的文件的绝对路径（精确匹配）",
            },
        },
        "required": ["path"],
    }
    risk_level = "medium"
    requires_confirm = True
    side_effects = ["delete_file"]
    root_hints = ["note", "pdf", "research"]

    def effective_target_path(self, args: dict) -> str | None:
        """导出写互斥键：被删的那个路径（基类默认行为，显式声明以表明契约）。

        Args:
            args: dict，已解析的工具调用参数

        Returns:
            目标文件的绝对路径；无 path 参数时 None。
        """
        return args.get("path")

    def execute(self, path: str) -> ToolResult:
        """删除文件；不存在或目标是目录时返回可行动的错误文本。

        Args:
            path: str，要删除的文件的绝对路径

        Returns:
            ToolResult；删除成功时 completion 供终端渲染完成行。
        """
        p = Path(path)
        if not p.exists():
            return ToolResult(text=f"文件不存在，未删除任何东西: {path}", is_error=True)
        if p.is_dir():
            return ToolResult(
                text=f"目标是目录，本工具只删文件；请给出目录内的具体文件路径: {path}",
                is_error=True)
        try:
            p.unlink()
        except OSError as e:
            return ToolResult(text=f"删除失败: {e}", is_error=True)
        return ToolResult(text=f"已删除 {path}", completion=f"File deleted: {path}")
