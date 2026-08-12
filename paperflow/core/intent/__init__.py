# paperflow/core/intent/__init__.py
"""
意图识别框架服务——包级统一导出。

本包包含意图识别所需的全部组件：路由契约与输出契约（schemas）、编码器与
混合索引（encoders）、路由器与输入判别（routing）、级联管线（pipeline）、
跨轮会话状态（conversation_state）。这里集中导出公开接口，供外部调用方从
单一入口导入。
"""
from paperflow.core.intent.schemas.intent import IntentType, IntentStep, IntentOutput, IntentionResult
from paperflow.core.intent.schemas.route import Route, RouteChoice
from paperflow.core.intent.encoders.dense import DenseEncoder, FixedDenseEncoder
from paperflow.core.intent.encoders.bm25 import JiebaTokenizer, BM25Encoder
from paperflow.core.intent.encoders.index import HybridLocalIndex
from paperflow.core.intent.routing.router import HybridRouter
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.routing.route_loader import load_routes
from paperflow.core.intent.conversation_state import ConversationState, PendingClarification

__all__ = [
    "IntentType", "IntentStep", "IntentOutput", "IntentionResult",
    "Route", "RouteChoice",
    "DenseEncoder", "FixedDenseEncoder",
    "JiebaTokenizer", "BM25Encoder",
    "HybridLocalIndex", "HybridRouter",
    "IntentPipeline", "load_routes",
    "ConversationState", "PendingClarification",
]
