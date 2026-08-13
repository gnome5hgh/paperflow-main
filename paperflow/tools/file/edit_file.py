# paperflow/tools/file/edit_file.py
"""EditFileTool：定向修改既有笔记(search-replace,小范围改动)。

LLM 只需输出变更部分(省 token),且不误伤无关内容。风险为 medium(与 write_file
对齐):限笔记根目录 + 需用户确认,安全性由路径限制与确认保证。写后调用索引热更新
钩子,与 WriteFileTool 保持索引一致。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service
from paperflow.tools.file._constants import NOTE_ROOTS
from paperflow.tools.file.atomic import atomic_write


class EditFileTool(Tool):
    name = "edit_file"
    description = "修改既有笔记（定向替换 search-replace；小范围改动，须精确匹配 old_text）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "文件绝对路径"},
            "old_text": {"type": "string", "format": "content", "description": "要替换的原文（须精确匹配，可先 grep 确认）"},
            "new_text": {"type": "string", "format": "content", "description": "替换后的文本"},
        },
        "required": ["path", "old_text", "new_text"],
    }
    risk_level = "medium"                      # 与 write_file 对齐:定向替换 + 限笔记根 + 确认
    requires_confirm = True
    allowed_roots = NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, old_text: str, new_text: str) -> ToolResult:
        """在文件里精确替换一处 old_text 为 new_text;不唯一或不存在时拒绝并给指引。

        查找用 str.count 判断唯一性——锚点必须唯一,避免替换错位置。
        """
        # 空 old_text 守卫:str.count("") 恒大于 1,会误入"多命中"分支报出令人困惑的错,
        # 直接明示参数错误。
        if not old_text:
            return ToolResult(text="old_text 不能为空，请提供要替换的原文")
        p = Path(path)
        if not p.exists():
            return ToolResult(text=f"文件不存在: {path}")
        content = p.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            return ToolResult(text="未找到要替换的文本，请先用 read_file/grep 确认当前内容")
        if count > 1:
            return ToolResult(text=f"待替换文本出现 {count} 次，请提供更长的唯一锚点")
        atomic_write(p, content.replace(old_text, new_text))
        get_rag_service().index_document(str(p))
        return ToolResult(text=f"已编辑 {path}", completion=f"File edited: {path}")
