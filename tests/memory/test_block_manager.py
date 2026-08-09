"""BlockManager 测试：CRUD + limit 校验 + read_only + 乐观锁 + checkpoint/restore。"""
import tempfile
from pathlib import Path

import pytest

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager


def _bm():
    tmp = tempfile.mkdtemp()
    return BlockManager(MemoryDB(Path(tmp) / "memory.db"))


def test_create_and_get():
    bm = _bm()
    b = bm.create_block("persona", "身份")
    assert bm.get_block(b.id).value == "身份"
    assert bm.get_block_by_label("persona").label == "persona"
    assert bm.get_block_by_label("nope") is None


def test_update_limit_enforced():
    bm = _bm()
    b = bm.create_block("persona", "身份", limit=10)
    with pytest.raises(ValueError, match="character limit"):
        bm.update_block_value("persona", "x" * 20)


def test_update_read_only_rejected():
    bm = _bm()
    bm.create_block("persona", "身份", read_only=True)
    with pytest.raises(ValueError, match="read.only"):
        bm.update_block_value("persona", "新身份")


def test_version_increments_and_checkpoint():
    bm = _bm()
    bm.create_block("persona", "身份")
    bm.update_block_value("persona", "身份v2")
    b = bm.get_block_by_label("persona")
    assert b.version == 2
    hist = bm._db_history("persona")
    assert len(hist) == 1 and hist[0]["value"] == "身份"   # 改动前快照


def test_restore_undo_redo():
    bm = _bm()
    bm.create_block("persona", "v1")
    bm.update_block_value("persona", "v2")
    bm.update_block_value("persona", "v3")
    hist = bm._db_history("persona")
    restored = bm.restore_block(hist[0]["id"])
    assert restored.value == "v1"     # 撤回到第一次改动前
