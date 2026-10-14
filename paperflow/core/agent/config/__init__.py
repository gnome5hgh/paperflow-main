"""agent 域组件的自有参数。

与顶层 `paperflow/config/` 分工：那里是「与 config.yaml 同构、可经 env 派生」的
全局可调参数；这里是**组件自己的行为参数**，由注册表在加载时构造，不进 config.yaml。
"""
from paperflow.core.agent.config.agent_config import AgentConfig

__all__ = ["AgentConfig"]
