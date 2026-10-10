"""Tool 抽象：所有 Agent 可调用工具的契约。

权限最小化设计:
- 每个 Tool 声明 ``risk_level``，由策略引擎根据风险等级决定放行/拒绝/要求确认
- ``ToolResult.summary`` 字段预留，供记忆系统写入结构化摘要
- Tool 实例统一通过 ``agents/<name>/tools.py`` 模块级 ``TOOLS`` 列表暴露，
  由 AgentRegistry 通过 importlib 动态加载

按角色分文件：``base.py``（Tool ABC 本体）、``result.py``（执行结果对象）、
``validation.py``（安全元数据校验）。消费方一律从本包取（``from paperflow.core.tool
import Tool, ToolResult``），不关心内部怎么分文件。
"""
from paperflow.core.tool.base import Tool
from paperflow.core.tool.result import ToolResult
from paperflow.core.tool.validation import validate_tool

__all__ = ["Tool", "ToolResult", "validate_tool"]
