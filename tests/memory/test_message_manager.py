"""MessageManager 测试：wire→schema 落盘、回放、检索、user 消息列表。"""
import tempfile
from pathlib import Path

from paperflow.core.llm import Message as WireMessage
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.message_manager import MessageManager


def _mm():
    tmp = tempfile.mkdtemp()
    return MessageManager(MemoryDB(Path(tmp) / "memory.db"))


def test_add_message_converts_and_persists():
    mm = _mm()
    saved = mm.add_message("sess_1", WireMessage(role="user", content="hi"))
    assert saved.id.startswith("message-") and saved.created_at is not None
    rows = mm.get_messages_by_agent_id("sess_1")
    assert len(rows) == 1 and rows[0].content == "hi"


def test_add_message_sanitizes_surrogates():
    """信任边界：带代理码点的消息落盘前清洗，不炸严格 UTF-8 写入。

    ask_user 回答等路径不经 agent.run 清洗，代理（surrogateescape 残留）会在此
    打爆 sqlite 写入——add_message 是全部消息落盘的单点，在此堵漏。
    """
    mm = _mm()
    mm.add_message("sess_1", WireMessage(role="user", content="a\udce4b"))
    rows = mm.get_messages_by_agent_id("sess_1")
    assert rows[0].content == "a�b"


def test_get_in_context_messages_returns_all_persisted():
    mm = _mm()
    mm.add_message("sess_1", WireMessage(role="user", content="a"))
    mm.add_message("sess_1", WireMessage(role="assistant", content="b"))
    in_ctx = mm.get_in_context_messages("sess_1")
    assert [m.content for m in in_ctx] == ["a", "b"]


def test_search_messages():
    mm = _mm()
    mm.add_message("sess_1", WireMessage(role="assistant", content="阅读 GraphCL 论文"))
    mm.add_message("sess_1", WireMessage(role="assistant", content="检索异构图"))
    hits = mm.search_messages("sess_1", "GraphCL")
    assert len(hits) == 1 and "GraphCL" in hits[0].content


def test_size_and_list_user_messages():
    mm = _mm()
    mm.add_message("sess_1", WireMessage(role="user", content="u1"))
    mm.add_message("sess_1", WireMessage(role="assistant", content="a1"))
    assert mm.size("sess_1") == 2
    users = mm.list_user_messages_for_agent("sess_1")
    assert [m.content for m in users] == ["u1"]


def test_content_json_like_string_roundtrips_as_string():
    """锁定：JSON 样式的字符串内容（如 StructuredOutput 的 {"query": ...}）在
    落盘→回放后仍保持字符串原样——不被 _row_to_schema 误 json.loads 成 dict/list
    而触发 Message.content（str | None）类型校验失败。"""
    mm = _mm()
    mm.add_message("sess_1", WireMessage(role="user", content='{"query": "GraphCL"}'))
    rows = mm.get_messages_by_agent_id("sess_1")
    assert rows[0].content == '{"query": "GraphCL"}'
    assert isinstance(rows[0].content, str)
