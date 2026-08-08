"""ConversationState 状态容器测试（Layer 4 跨轮状态）。"""
from paperflow.core.conversation_state import ConversationState, PendingClarification
from paperflow.core.intent.intent_schema import IntentType


def test_session_defaults():
    s = ConversationState()
    assert s.prev_intent is None
    assert s.prev_user_input == ""
    assert s.pending_intent is None


def test_session_fields_update():
    s = ConversationState()
    s.prev_intent = IntentType.SEARCH_PAPER
    s.prev_user_input = "搜索 circRNA"
    assert s.prev_intent == IntentType.SEARCH_PAPER
    assert s.prev_user_input == "搜索 circRNA"


def test_pending_round_chains():
    """round 链式累计：REPL 重建时用旧值 +1，不重置为 0（D9 防死循环关键）。"""
    p = PendingClarification(question="要哪个？", original_input="整理这篇")
    p.round += 1
    assert p.round == 1
    p.round += 1
    assert p.round == 2
