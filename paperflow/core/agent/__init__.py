# paperflow/core/agent/__init__.py
"""Agent 域包 —— ReAct 运行时、注册表与行为基座。

公共出口（下游一律 `from paperflow.core.agent import ...`，勿深入子模块）：
- ``Agent`` / ``MaxTurnsExceeded`` ← ``services/runtime``
- ``AgentRegistry`` ← ``services/registry``
- ``AgentEvent`` / ``AgentConfig`` ← domain/dto 与 config（跨包契约对象）
- ``AgentEventKind`` ← constants（引擎事件类型词表）
- ``get_run_state`` / ``get_session_state`` ← ``services/state``（运行期状态容器取用点）

`state` 的两个取用函数在此导出，是为了让 `tools/` 侧不再深引
`core.agent.services.state` —— 包门面是唯一入口，内部模块布局才可自由调整。

依赖方向声明：本包可依赖 core.llm / core.skills / core.tool / core.common /
core.memory / core.security / core.intent；反向（它们 import 本包）不被允许。
"""

from paperflow.core.agent.constants import AgentEventKind
from paperflow.core.agent.config import AgentConfig
from paperflow.core.agent.domain import AgentEvent
from paperflow.core.agent.services.registry import AgentRegistry
from paperflow.core.agent.services.runtime import Agent, MaxTurnsExceeded
from paperflow.core.agent.services.state import get_run_state, get_session_state

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentRegistry",
    "MaxTurnsExceeded",
    "AgentEvent",
    "AgentEventKind",
    "get_run_state",
    "get_session_state",
]
