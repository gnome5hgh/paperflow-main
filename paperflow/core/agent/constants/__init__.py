"""Agent 域的跨模块词汇。

引擎事件类型在 runtime（产出）与终端渲染/spawn 遥测（消费）之间流转，是这个包
现在收的唯一一项。本包只做再导出，消费方一律从 `paperflow.core.agent.constants`
取，不关心内部怎么分文件。
"""
from paperflow.core.agent.constants.enums import AgentEventKind

__all__ = ["AgentEventKind"]
