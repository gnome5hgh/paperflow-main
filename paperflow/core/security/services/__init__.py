"""security 服务：与中间件无关的独立防护能力。

`network.py` 是**统一出网出口**——配置化端点做 host 绑定，外部内容带来的 URL 做
SSRF 校验，两类都返回目标 host 供审计登记；它被 `tools/` 与各模型客户端直接调用，
不属于中间件洋葱。
"""
from paperflow.core.security.services.classifier import (
    ClassifierUnavailable,
    ClassifierVerdict,
    SemanticClassifier,
    build_classifier,
)
from paperflow.core.security.services.network import (
    EndpointBindingError,
    SSRFError,
    assert_endpoint_binding,
    check_egress,
    endpoint_guard,
    resolve_url_target,
    validate_url_target,
)

__all__ = [
    "SSRFError", "EndpointBindingError",
    "validate_url_target", "resolve_url_target",
    "assert_endpoint_binding", "endpoint_guard", "check_egress",
    "SemanticClassifier", "ClassifierVerdict", "ClassifierUnavailable",
    "build_classifier",
]
