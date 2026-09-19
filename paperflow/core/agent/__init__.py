# paperflow/core/agent/__init__.py
"""Agent 域包 —— ReAct 运行时、注册表与行为基座。

公共出口（下游一律 `from paperflow.core.agent import ...`，勿深入子模块）：
- ``Agent`` / ``StreamEvent`` / ``MaxTurnsExceeded`` ← runtime
- ``AgentRegistry`` / ``AgentConfig`` ← registry
依赖方向声明：本包可依赖 core.llm / core.skills / core.tool / core.frontmatter /
core.memory / core.security / core.intent；反向（它们 import 本包）不被允许。
"""

from paperflow.core.agent.registry import AgentConfig, AgentRegistry
from paperflow.core.agent.runtime import Agent, MaxTurnsExceeded, StreamEvent

__all__ = ["Agent", "AgentConfig", "AgentRegistry", "MaxTurnsExceeded", "StreamEvent"]
