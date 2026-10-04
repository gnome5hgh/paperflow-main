"""RAG 工具运行时上下文 + 模块级访问器（仿 memory 工具的 runtime_context 先例）。

RagRetrieveTool 需要对话历史做 condense 改写，但历史是会话态而非 LLM 工具参数
——不能指望模型自觉传参。工具不持有上下文；execute 时经 get_rag_context() 取
运行时绑定的 history_provider。CLI 启动装配完服务层后 set_rag_context 绑定一次；
未绑定返回 None，工具据此跳过 condense 只做扩写。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

__all__ = ["RagToolsContext", "set_rag_context", "get_rag_context"]


@dataclass
class RagToolsContext:
    """RAG 工具共享上下文（execute 时经 get_rag_context() 取得）。

    history_provider: 返回最近对话消息列表（元素为带 .role/.content 的对象，
    如 core.llm.client.Message），只含 user/assistant 角色；抛异常视为无历史。
    """

    history_provider: Callable[[], list] | None = None


_rag_context: RagToolsContext | None = None


def set_rag_context(ctx: RagToolsContext | None) -> None:
    """绑定/清空运行时上下文（CLI 装配后调用；测试传 None 隔离）。"""
    global _rag_context
    _rag_context = ctx


def get_rag_context() -> RagToolsContext | None:
    """返回当前绑定的上下文；未绑定返回 None（工具据此跳过 condense）。"""
    return _rag_context
