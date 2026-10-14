"""agent 域的层间契约对象。

- ``StreamEvent``：流式事件（runtime 产出，终端渲染与 spawn 遥测消费）

插件配置 `AgentConfig` 是**组件自己的参数**，在 `core/agent/config/`。
"""
from paperflow.core.agent.domain.dto.stream_event import StreamEvent

__all__ = ["StreamEvent"]
