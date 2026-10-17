"""只读召回工具包：会话检索 + 工具结果回取。

召回是读操作，不是领域产物写入；它从记忆写入包里拆出来，便于装配给所有
需要核对原文的 agent，而不必为一次关键词检索派发 memory-agent。
"""
from paperflow.tools.recall.conversation_search import ConversationSearchTool
from paperflow.tools.recall.read_tool_result import ReadToolResultTool

__all__ = ["get_recall_tools", "ConversationSearchTool", "ReadToolResultTool"]


def get_recall_tools() -> list:
    """返回只读召回工具实例列表（会话检索 + 工具结果回取）。

    Returns:
        list[Tool]，两个只读工具。
    """
    return [ConversationSearchTool(), ReadToolResultTool()]
