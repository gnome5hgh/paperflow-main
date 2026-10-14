"""StructuredOutput 的行为参数。

与 `LLMConfig` 那类「config.yaml 同构、可经 env 派生」的可调参数不同：这里是
**组件自己的行为参数**，由各消费方在调用点各给一套（查询改写要 0 次重试、
看图分析要 1 次、默认 2 次），不进 config.yaml。同类先例见 `AgentConfig`
（`core/agent/config/`）与 `SkillConfig`（`core/skills/config/`）。
"""
from dataclasses import dataclass

__all__ = ["StructuredOutputConfig"]


@dataclass
class StructuredOutputConfig:
    """StructuredOutput 的行为参数：重试次数、LLM 采样温度、是否关思维链、
    是否强制 JSON 模式、schema 递归展开的最大深度。

    Attributes:
        max_retries: int，校验失败后的重试次数
        temperature: float，采样温度（0.0 = 确定性）
        disable_thinking: bool，是否关闭思维链（思维链尾随散文会破坏 JSON）
        json_mode: bool，是否强制 JSON 输出格式
        max_schema_depth: int，schema 递归展开的最大深度（防自引用模型无限递归）
    """

    max_retries: int = 2
    temperature: float = 0.0
    disable_thinking: bool = True
    json_mode: bool = True
    max_schema_depth: int = 3
