# tests/intent/test_intent_schema.py
"""IntentType / IntentStep / IntentOutput / IntentionResult 契约测试（spec Section 4）。"""

import pytest
from pydantic import ValidationError

from paperflow.core.intent.intent_schema import (
    IntentOutput, IntentStep, IntentType, IntentionResult,
)


class TestIntentType:
    def test_current_set(self):
        # spec Section 3/4：Layer 1 简化集合恰为 5 个，value 即路由名
        values = {t.value for t in IntentType}
        assert values == {"search_paper", "generate_note", "ask_question",
                          "manage_memory", "general"}

    def test_str_enum(self):
        # 继承 str，value 可被 YAML / JSON 直接序列化
        assert IntentType.SEARCH_PAPER == "search_paper"


class TestIntentStep:
    def test_values(self):
        # 4 个产出阶段枚举（Stage 0/1 Layer 4 实现，Stage 2/3 真实）
        values = {s.value for s in IntentStep}
        assert values == {"entities", "followup", "router", "llm"}


class TestIntentOutput:
    def test_full_construction(self):
        out = IntentOutput(
            intent_type=IntentType.SEARCH_PAPER,
            confidence=0.7,
            entities={"query": "circRNA"},
            rewritten_query="circRNA",
            source=IntentStep.ROUTER,
        )
        assert out.intent_type == IntentType.SEARCH_PAPER
        assert out.source == IntentStep.ROUTER
        assert out.rewritten_query == "circRNA"

    def test_defaults(self):
        # 缺省值：entities={} / rewritten_query="" / prev_intent=None
        out = IntentOutput(
            intent_type=IntentType.GENERAL, confidence=0.0,
            source=IntentStep.LLM,
        )
        assert out.entities == {}
        assert out.rewritten_query == ""
        assert out.prev_intent is None

    def test_confidence_range_enforced(self):
        # pydantic 范围约束：越界值抛 ValidationError（LLM 可能输出越界）
        with pytest.raises(ValidationError):
            IntentOutput(intent_type=IntentType.GENERAL, confidence=1.5,
                         source=IntentStep.LLM)


class TestIntentionResult:
    def test_construction(self):
        # 扁平三字段：query_rewrite 缺省为空串
        r = IntentionResult(intent_type=IntentType.GENERAL, confidence=0.0)
        assert r.query_rewrite == ""

    def test_confidence_range_enforced(self):
        # 范围约束同样作用于 LLM 兜底 schema（StructuredOutput 不校验范围）
        with pytest.raises(ValidationError):
            IntentionResult(intent_type=IntentType.GENERAL, confidence=-0.2)
