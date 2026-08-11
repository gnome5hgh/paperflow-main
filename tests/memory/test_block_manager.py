"""BlockManager 测试：CRUD + limit 校验 + read_only + 乐观锁 + checkpoint/restore。"""
import tempfile
from pathlib import Path

import pytest

from paperflow.core.memory.constants import DEFAULT_PERSONA, DEFAULT_HUMAN
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.memory import Memory
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


def test_delete_read_only_rejected():
    """评审 I-5：delete_block 与 update_block_value 一致，拒绝删除 read_only 保护块。"""
    bm = _bm()
    bm.create_block("persona", "身份", read_only=True)
    b = bm.get_block_by_label("persona")
    with pytest.raises(ValueError, match="read.only"):
        bm.delete_block(b.id)
    assert bm.get_block_by_label("persona") is not None   # 块未被删


def test_delete_removes_block():
    bm = _bm()
    b = bm.create_block("feedback_testing", "规则")
    bm.delete_block(b.id)
    assert bm.get_block_by_label("feedback_testing") is None


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


def test_ensure_default_blocks_creates_both_on_empty():
    bm = _bm()
    created = bm.ensure_default_blocks()
    assert created == ["persona", "human"]
    assert bm.get_block_by_label("persona").value == DEFAULT_PERSONA
    assert bm.get_block_by_label("human").value == DEFAULT_HUMAN


def test_ensure_default_blocks_idempotent_never_overwrites():
    bm = _bm()
    bm.create_block("persona", "自定义身份")
    created = bm.ensure_default_blocks()
    assert created == ["human"]                    # persona 已存在 → 只补 human
    assert bm.get_block_by_label("persona").value == "自定义身份"   # 不覆盖
    assert bm.ensure_default_blocks() == []        # 二次调用 no-op


def test_ensure_default_blocks_backfills_migrated_db():
    bm = _bm()
    bm.create_block("user_role", "用户是研究生")
    created = bm.ensure_default_blocks()
    assert created == ["persona", "human"]
    assert bm.get_block_by_label("user_role").value == "用户是研究生"  # 原块不动


def test_seeded_blocks_render_in_compile():
    bm = _bm()
    bm.ensure_default_blocks()
    text = Memory(blocks=bm.list_blocks()).compile()
    assert '<block name="persona">' in text
    assert '<block name="human">' in text
