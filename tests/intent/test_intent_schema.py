# tests/intent/test_intent_schema.py
"""IntentType / IntentStep / IntentOutput / IntentionResult 契约测试（ADR 0007）。"""

import pytest
from pydantic import ValidationError

from paperflow.core.intent.intent_schema import (
    IntentOutput, IntentStep, IntentType, IntentionResult,
)
from paperflow.core.intent.route import RouteChoice


def test_intent_type_members():
    # ADR 0007：7 类细粒度意图，value 与路由名一致
    assert set(IntentType._value2member_map_) == {
        "search_paper", "download_paper", "read_paper", "answer_question",
        "query_notes", "generate_note", "chitchat",
    }


def test_intent_type_is_str_enum():
    # 继承 str，value 可被 YAML / JSON 直接序列化
    assert IntentType.SEARCH_PAPER.value == "search_paper"
    assert str(IntentType.SEARCH_PAPER) == "IntentType.SEARCH_PAPER"


def test_intent_step():
    step = IntentStep(
        intent=IntentType.SEARCH_PAPER,
        entities={"query": "attention is all you need", "download": False},
    )
    assert step.intent == IntentType.SEARCH_PAPER
    assert step.entities["query"] == "attention is all you need"
    assert step.entities["download"] is False


def test_intent_step_default_entities():
    # entities 有默认值（空 dict），单意图 step 可不带实体
    step = IntentStep(intent=IntentType.CHITCHAT)
    assert step.entities == {}


def test_intent_output_single_step():
    # 普通输入：1 个 step，Stage 2 命中，相似度写入 similarity_score
    out = IntentOutput(
        steps=[IntentStep(intent=IntentType.SEARCH_PAPER, entities={"query": "xxx"})],
        confidence=0.92,
        source="stage2",
        similarity_score=0.92,
        raw_input="搜索 xxx 论文",
    )
    assert len(out.steps) == 1
    assert out.steps[0].intent == IntentType.SEARCH_PAPER
    assert out.confidence == 0.92
    assert out.similarity_score == 0.92
    assert out.raw_input == "搜索 xxx 论文"


def test_intent_output_clarification_path():
    # steps=[] + clarification → ask_user 澄清闭环（Supervisor 消费规则）
    out = IntentOutput(
        source="stage3",
        clarification="你是想读全文还是只问某个问题？",
        raw_input="帮我看看这篇",
    )
    assert out.steps == []
    assert out.clarification is not None
    assert out.reply_suggestion is None


def test_intent_output_reply_suggestion_path():
    # steps=[] + reply_suggestion → 直接回复，不 spawn
    out = IntentOutput(
        source="stage2",
        reply_suggestion="我是学术助手，有什么论文相关问题吗？",
        raw_input="在吗",
    )
    assert out.steps == []
    assert out.reply_suggestion is not None
    assert out.clarification is None


def test_intent_output_requires_source_and_raw_input():
    # source 与 raw_input 必填，缺失时校验失败
    with pytest.raises(ValidationError):
        IntentOutput(steps=[])


def test_intention_result_with_route_choice():
    # Stage 2 命中：IntentionResult 携带路由原始决策（相似度供审计）
    choice = RouteChoice(name="search_paper", similarity_score=0.88)
    out = IntentOutput(
        steps=[IntentStep(intent=IntentType.SEARCH_PAPER, entities={})],
        source="stage2",
        similarity_score=0.88,
        raw_input="搜索 xxx",
    )
    result = IntentionResult(output=out, route_choice=choice)
    assert result.output is out
    assert result.route_choice.name == "search_paper"
    assert result.near_misses == []


def test_intention_result_near_misses():
    # Stage 3 兜底：注入未达阈值的近失候选（top 分数）
    near = [
        RouteChoice(name="download_paper", similarity_score=0.41),
        RouteChoice(name="read_paper", similarity_score=0.32),
    ]
    out = IntentOutput(
        source="stage3",
        new_intent_candidate={"name": "summarize", "utterances": ["帮我总结这篇"]},
        raw_input="帮我总结这篇论文",
    )
    result = IntentionResult(output=out, near_misses=near)
    assert len(result.near_misses) == 2
    assert result.near_misses[0].similarity_score == 0.41
    assert result.output.new_intent_candidate["name"] == "summarize"
    assert result.route_choice is None
