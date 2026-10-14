"""Tool 抽象：所有 Agent 可调用工具的契约。

权限最小化设计:
- 每个 Tool 声明 ``risk_level``，由策略引擎根据风险等级决定放行/拒绝/要求确认
- ``ToolResult.summary`` 字段预留，供记忆系统写入结构化摘要
- Tool 实例统一通过 ``agents/<name>/tools.py`` 模块级 ``TOOLS`` 列表暴露，
  由 AgentRegistry 通过 importlib 动态加载

包内按角色分子包：``domain/``（本领域的公共词汇：``entity/`` 契约与 ``dto/`` 结果载体）、
``services/``（加载期校验）、``constants/``（summary 的跨模块键）。
消费方一律从本包取（``from paperflow.core.tool import Tool, ToolResult``），
不关心内部怎么分文件。
"""
from paperflow.core.tool.constants import (
    SUMMARY_DECISION,
    SUMMARY_TERMINAL,
    SUMMARY_VIOLATIONS,
)
from paperflow.core.tool.domain import WRITE_EFFECTS, Tool, ToolResult, is_writer
from paperflow.core.tool.services import validate_tool

__all__ = [
    "SUMMARY_DECISION",
    "SUMMARY_TERMINAL",
    "SUMMARY_VIOLATIONS",
    "Tool",
    "ToolResult",
    "WRITE_EFFECTS",
    "is_writer",
    "validate_tool",
]
