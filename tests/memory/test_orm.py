"""存储层测试：MemoryDB 建表 + blocks/block_history/messages/archival_passages CRUD。"""
import json
import tempfile
from pathlib import Path

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.orm import block as block_orm
from paperflow.core.memory.orm import message as message_orm
from paperflow.core.memory.orm import passage as passage_orm
from paperflow.core.memory.schemas.block import Block
from paperflow.core.memory.schemas.message import Message, MessageRole
from paperflow.core.memory.schemas.passage import Passage


def _db():
    tmp = tempfile.mkdtemp()
    return MemoryDB(Path(tmp) / "memory.db")


def test_init_schema_creates_tables():
    db = _db()
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r[0] for r in tables}
    assert {"blocks", "block_history", "messages", "archival_passages"} <= names


def test_block_orm_crud_and_checkpoint():
    db = _db()
    b = Block.persona("身份")
    block_orm.insert_block(db, b, version=1)
    row = block_orm.select_block(db, b.id)
    assert row["label"] == "persona" and row["value"] == "身份"
    block_orm.checkpoint_block(db, b.id, "persona", "身份", 2000, None, {}, 1)
    hist = block_orm.select_block_history(db, b.id)
    assert len(hist) == 1


def test_message_orm_insert_and_select():
    db = _db()
    m = Message(id="message-1", role=MessageRole.tool, content="ok", tool_call_id="tc1")
    message_orm.insert_message(db, "sess_1", m)
    rows = message_orm.select_messages_by_agent(db, "sess_1")
    assert len(rows) == 1 and rows[0]["role"] == "tool"


def test_message_orm_search_like():
    db = _db()
    for i, text in enumerate(["阅读 GraphCL 论文", "检索异构图"]):
        message_orm.insert_message(db, "sess_1",
            Message(id=f"m{i}", role=MessageRole.assistant, content=text))
    hits = message_orm.search_messages(db, "sess_1", "GraphCL")
    assert len(hits) == 1 and "GraphCL" in hits[0]["content"]


def test_passage_orm_insert_soft_delete():
    db = _db()
    p = Passage(id="passage-1", text="结论", tags=["reading"])
    passage_orm.insert_passage(db, "sess_1", p)
    assert len(passage_orm.select_passages(db, "sess_1")) == 1
    passage_orm.soft_delete(db, "passage-1")
    assert len(passage_orm.select_passages(db, "sess_1")) == 0
