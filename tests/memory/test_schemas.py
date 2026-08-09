"""schemas 数据模型测试：Block/Memory/Message/Passage/AgentState 字段与构造器。"""
import pytest
from paperflow.core.memory.schemas.block import Block
from paperflow.core.memory.schemas.memory import Memory
from paperflow.core.memory.schemas.message import Message, MessageRole
from paperflow.core.memory.schemas.passage import Passage
from paperflow.core.memory.schemas.agent import AgentState


def test_block_constructors_and_fields():
    p = Block.persona("你是一名研究助手")
    assert p.label == "persona" and p.value == "你是一名研究助手"
    h = Block.human("用户是研究生")
    assert h.label == "human"
    b = Block.new("feedback_testing", "规则内容")
    assert b.label == "feedback_testing" and b.id.startswith("block-")
    assert b.limit == 2000 and b.read_only is False and b.metadata_ == {}


def test_block_human_persona_labels():
    assert Block.persona("x").label == "persona"
    assert Block.human("x").label == "human"


def test_memory_compile_renders_system_blocks():
    mem = Memory(blocks=[Block.persona("身份"), Block.human("用户"),
                         Block.new("feedback_a", "非system内容")])
    out = mem.compile()
    assert "<memory_blocks>" in out
    assert "persona" in out
    # 非 system/ 块不进 compile（MemFS 渐进暴露）
    assert "feedback_a" not in out


def test_memory_block_ops():
    mem = Memory(blocks=[Block.persona("身份")])
    b = mem.get_block("persona")
    assert b.value == "身份"
    assert mem.get_block("nope") is None
    mem.update_block_value("persona", "新身份")
    assert mem.get_block("persona").value == "新身份"
    assert len(mem.get_blocks()) == 1


def test_message_schema_fields():
    m = Message(id="message-1", role=MessageRole.assistant, content="hi",
                tool_calls=[], tool_call_id=None, step_id="s1", run_id="r1")
    assert m.role == MessageRole.assistant
    assert m.otid is None


def test_passage_schema_fields():
    p = Passage(id="passage-1", text="内容", tags=["reading"])
    assert p.is_deleted is False
    assert p.embedding is None


def test_agent_state_fields():
    st = AgentState(agent_id="sess_1", memory=Memory(blocks=[]))
    assert st.agent_id == "sess_1"
    assert st.message_ids == [] and st.name is None
