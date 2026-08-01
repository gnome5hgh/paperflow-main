# Task 8 回归测试：paperflow.core.intent 包级完整导出。
# 每个子模块（schema / intent_schema / dense_encoder / bm25_encoder /
# hybrid_index / hybrid_router / pipeline / route_loader）的公开符号都必须
# 能直接从包顶导入——外部消费方（Supervisor / 对照脚本）只依赖这一层。
from paperflow.core.intent import (
    IntentType, IntentStep, IntentOutput, IntentionResult,
    Route, RouteChoice,
    DenseEncoder, FixedDenseEncoder,
    JiebaTokenizer, BM25Encoder,
    HybridLocalIndex, HybridRouter,
    IntentPipeline, load_routes,
)


def test_all_exported():
    assert IntentType and IntentStep and IntentOutput and IntentionResult
    assert Route and RouteChoice
    assert DenseEncoder and FixedDenseEncoder
    assert JiebaTokenizer and BM25Encoder
    assert HybridLocalIndex and HybridRouter
    assert IntentPipeline and load_routes
