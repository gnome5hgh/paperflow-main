"""LLM 域组件的自有参数。

与顶层 `paperflow/config/` 分工：那里是「与 config.yaml 同构、可经 env 派生」的
全局可调参数（如 `LLMConfig`）；这里是**组件自己的行为参数**，由各消费方在调用点
各给一套，不进 config.yaml。
"""
from paperflow.core.llm.config.structured_output_config import StructuredOutputConfig

__all__ = ["StructuredOutputConfig"]
