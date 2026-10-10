"""agent 域的层间契约对象。

- ``AgentConfig``：插件配置（`AgentRegistry` 从 `agents/<name>/` 加载的产物，
  cli 与 skills 装配都要读）
- ``StreamEvent``：流式事件（runtime 产出，终端渲染与 spawn 遥测消费）
"""
from paperflow.core.agent.domain.dto.agent_config import AgentConfig
from paperflow.core.agent.domain.dto.stream_event import StreamEvent

__all__ = ["AgentConfig", "StreamEvent"]
