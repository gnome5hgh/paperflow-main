"""工具声明的加载期校验：安全元数据是否合法。

放在 services 是因为它是**行为**（在注册表加载时执行校验并抛错），而 `domain/` 只放
词汇与载体。
"""
from paperflow.core.tool.services.validation import validate_tool

__all__ = ["validate_tool"]
