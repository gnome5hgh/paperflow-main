"""工具结果外置：超阈值时把 ToolResult 换成指针。

外置发生在工具执行完成、after 钩子跑完之后：审计仍能看到原文，模型只看到指针。
指针里的预览足以判断是否值得取回全文；取回由 read_tool_result 完成。
"""
from __future__ import annotations

import json
import logging

from paperflow.core.tool.domain.dto.tool_result import ToolResult

logger = logging.getLogger(__name__)


def make_tool_result_offloader(store, settings):
    """构造工具结果外置回调。

    Args:
        store: ToolResultStore，原文存档。
        settings: ToolResultArchiveConfig，阈值与预览长度。

    Returns:
        可调用对象 ``(tool_name, tool_call_id, session_id, result, tool=None)``，
        返回可能被替换成指针的 ToolResult。
    """

    def offload(tool_name: str, tool_call_id: str, session_id: str,
                result: ToolResult, tool=None) -> ToolResult:
        """按阈值决定是否外置一条工具结果。

        Args:
            tool_name: 工具名。
            tool_call_id: 工具调用 ID。
            session_id: 会话标识。
            result: 原始 ToolResult。
            tool: Tool | None，用它的 max_result_size_chars 覆写阈值。

        Returns:
            原结果、指针结果，或明确截断的结果。
        """
        if result is None or result.is_error:
            return result
        if tool_name == "read_tool_result":
            return result
        text = result.text or ""
        ceiling = settings.persist_threshold_chars
        per_tool = getattr(tool, "max_result_size_chars", None) if tool is not None else None
        threshold = min(per_tool if per_tool is not None else ceiling, ceiling)
        if len(text) <= threshold:
            return result
        stored = store.put(session_id, tool_call_id, text)
        if stored is None:
            return ToolResult(
                text=(text[:threshold]
                      + f"\n…[工具结果过长（{len(text)} 字符），"
                        "未能保存到对象存储或本地，已截断]"),
                summary=result.summary,
                completion=result.completion,
                is_error=result.is_error,
            )
        archive_key, local_path = stored
        pointer = {
            "decision": "persisted",
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "size_chars": len(text),
            "archive_key": archive_key,
            "local_path": local_path,
            "preview": text[:settings.preview_chars],
            "hint": "用 read_tool_result(tool_call_id) 取回全文",
        }
        logger.debug("工具结果已外置: tool=%s call=%s size=%d",
                     tool_name, tool_call_id, len(text))
        return ToolResult(
            text=json.dumps(pointer, ensure_ascii=False),
            summary=result.summary,
            completion=result.completion,
            is_error=result.is_error,
        )

    return offload
