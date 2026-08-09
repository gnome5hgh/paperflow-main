"""AgentManager / ArchiveManager 测试。"""
import tempfile
from pathlib import Path

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.core.memory.services.archive_manager import ArchiveManager
from paperflow.core.memory.services.passage_manager import PassageManager


def _agents():
    tmp = tempfile.mkdtemp()
    db = MemoryDB(Path(tmp) / "memory.db")
    bm = BlockManager(db)
    mm = MessageManager(db)
    return AgentManager(db, bm, mm), bm


def test_create_and_get_agent():
    am, bm = _agents()
    st = am.create_agent("sess_1", name="research")
    assert st.agent_id == "sess_1" and st.name == "research"
    got = am.get_agent("sess_1")
    assert got.agent_id == "sess_1"


def test_refresh_memory_rebuilds_blocks():
    am, bm = _agents()
    st = am.create_agent("sess_1")
    bm.create_block("persona", "身份")
    am.refresh_memory("sess_1")
    got = am.get_agent("sess_1")
    assert got.memory.get_block("persona").value == "身份"


def test_archive_crud():
    tmp = tempfile.mkdtemp()
    db = MemoryDB(Path(tmp) / "memory.db")
    pm = PassageManager(db)
    arm = ArchiveManager(db, pm)
    arch = arm.create_archive("论文集", "共享集合")
    assert arch.name == "论文集"
    p = pm.insert_passage("sess_1", "内容")
    arm.add_passage(arch.id, p)
    assert len(arm.list_archives()) == 1
