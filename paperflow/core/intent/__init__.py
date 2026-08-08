# paperflow/core/intent/__init__.py
"""
意图识别框架服务——包级统一导出。

本包包含意图识别所需的全部组件：路由契约与输出契约（schema / intent_schema）、
编码器（dense_encoder / bm25_encoder）、混合索引（hybrid_index）、
路由器（hybrid_router）、级联管线（pipeline）、意图知识库加载（route_loader）。
这里集中导出公开接口，供外部调用方从单一入口导入。
"""
from paperflow.core.intent.intent_schema import (
    IntentType, IntentStep, IntentOutput, IntentionResult,
)
from paperflow.core.intent.schema import Route, RouteChoice
from paperflow.core.intent.dense_encoder import DenseEncoder, FixedDenseEncoder
from paperflow.core.intent.bm25_encoder import JiebaTokenizer, BM25Encoder
from paperflow.core.intent.hybrid_index import HybridLocalIndex
from paperflow.core.intent.hybrid_router import HybridRouter
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.route_loader import load_routes

__all__ = [
    "IntentType", "IntentStep", "IntentOutput", "IntentionResult",
    "Route", "RouteChoice",
    "DenseEncoder", "FixedDenseEncoder",
    "JiebaTokenizer", "BM25Encoder",
    "HybridLocalIndex", "HybridRouter",
    "IntentPipeline", "load_routes",
]
