"""agent 领域模型：跨包共享的契约对象。

`Agent`／`AgentRegistry`／`RunState` 是服务与运行时状态，留在包里各自的模块；
这里只放「数据长什么样」的对象。
"""
from paperflow.core.agent.domain.dto import AgentConfig, StreamEvent

__all__ = ["AgentConfig", "StreamEvent"]
