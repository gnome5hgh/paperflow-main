# paperflow/tools/file/edit_file.py
"""EditFileTool：定向修改既有笔记(search-replace,小范围改动)。

LLM 只需输出变更部分(省 token),且不误伤无关内容。安全边界由中间件强制:path 可为
任意绝对路径,敏感路径黑名单(workspace/audit、.git 等)由 WorkspacePolicyMiddleware
拦截。风险为 medium(与 write_file 对齐),需用户确认。写后调用索引热更新钩子,与
WriteFileTool 保持索引一致。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service
from paperflow.tools.file._constants import NOTE_HINTS
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
    risk_level = "medium"                      # 与 write_file 对齐:定向替换 + medium 风险确认
    requires_confirm = True
    root_hints = NOTE_HINTS
    side_effects = ["write_file"]
    #: 注入本次 run 的状态容器，用于把落盘路径登记进产物账本
    wants_run_state = True

    def execute(self, path: str, old_text: str, new_text: str,
                _run_state=None) -> ToolResult:
        """在文件里精确替换一处 old_text 为 new_text;不唯一或不存在时拒绝并给指引。

        查找用 str.count 判断唯一性——锚点必须唯一,避免替换错位置。
        _run_state 为本次 run 的状态容器（未注入时为 None）：替换落盘成功后把路径
        登记进产物账本；索引失败不影响登记。
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
        if _run_state is not None:
            _run_state.artifacts[str(p)] = "edit_file"
        note = ""
        try:
            get_rag_service().index_document(str(p))
        except Exception as e:
            # Milvus 是外部服务可能未启动：索引失败只降级为提示，不掩盖已成功的编辑
            note = f"（索引失败：{e}）"
        return ToolResult(text=f"已编辑 {path}{note}", completion=f"File edited: {path}")
