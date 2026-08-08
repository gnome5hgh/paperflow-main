# paperflow/tools/edit_file.py
"""EditFileTool：定向修改既有笔记（search-replace，小范围改动）。

2026-08-06：从"全量覆盖"改为"定向替换"——LLM 只需输出变更部分（省 token），
且不误伤无关内容。risk 从 high 降为 medium（与 write_file 对齐）：限 note 根 +
requires_confirm=True，用户对每次修改确认，安全性由路径限制 + 确认保证。
写后调 index_document() 热更新钩子（与 WriteFileTool 同款，索引一致维护）。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service
from paperflow.tools.file._constants import NOTE_ROOTS


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
    risk_level = "medium"                      # 与 write_file 对齐（2026-08-06）；定向替换 + 限 note 根 + 确认
    requires_confirm = True
    allowed_roots = NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, old_text: str, new_text: str) -> ToolResult:
        # 空 old_text 守卫：str.count("") 恒等于 len+1 > 1，会误入"多命中"分支，
        # 返回的报错让模型困惑（Minor 10）。直接明示参数错误。
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
        p.write_text(content.replace(old_text, new_text), encoding="utf-8")
        get_rag_service().index_document(str(p))
        return ToolResult(text=f"已编辑 {path}")
