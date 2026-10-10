"""security 服务：与中间件无关的独立防护能力。

目前只有 SSRF 防护（`network.py`）——它被 `tools/` 侧直接调用，不属于中间件洋葱。
"""
from paperflow.core.security.services.network import (
    SSRFError,
    resolve_url_target,
    validate_url_target,
)

__all__ = ["SSRFError", "validate_url_target", "resolve_url_target"]
