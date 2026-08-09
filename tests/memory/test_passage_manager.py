"""PassageManager 测试：archival passage 写入/检索/软删除/tags。"""
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paperflow.core.memory.orm import passage as passage_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.passage import Passage
from paperflow.core.memory.services.passage_manager import PassageManager


def _pm():
    tmp = tempfile.mkdtemp()
    return PassageManager(MemoryDB(Path(tmp) / "memory.db"))


def test_insert_and_count():
    pm = _pm()
    p = pm.insert_passage("sess_1", "论文结论", tags=["reading"])
    assert p.id.startswith("passage-")
    assert pm.agent_passage_size("sess_1") == 1


def test_search_by_tags():
    pm = _pm()
    pm.insert_passage("sess_1", "GraphCL 结论", tags=["reading"])
    pm.insert_passage("sess_1", "异构图检索", tags=["paper"])
    # 无 embedder 时按 tags 过滤
    hits = pm.search_passages("sess_1", "", tags=["paper"])
    assert len(hits) == 1 and "异构图" in hits[0].text


def test_delete_soft():
    pm = _pm()
    p = pm.insert_passage("sess_1", "内容")
    pm.delete_passage(p.id)
    assert pm.agent_passage_size("sess_1") == 0


def test_unique_tags():
    pm = _pm()
    pm.insert_passage("sess_1", "a", tags=["reading"])
    pm.insert_passage("sess_1", "b", tags=["paper", "review"])
    assert set(pm.get_unique_tags("sess_1")) == {"reading", "paper", "review"}


def test_search_by_time_range():
    # 回归锁定：created_at 经 _row_to_schema 已是 datetime，时间过滤必须转 ISO 字符串
    # 比较，否则 datetime >= str 直接 TypeError。
    pm = _pm()
    passage_orm.insert_passage(pm.db, "sess_1",
                               Passage(text="早期笔记", created_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)))
    passage_orm.insert_passage(pm.db, "sess_1",
                               Passage(text="后期笔记", created_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)))
    early = pm.search_passages("sess_1", "", end_datetime="2026-08-02T00:00:00+00:00")
    assert len(early) == 1 and "早期" in early[0].text
    late = pm.search_passages("sess_1", "", start_datetime="2026-08-02T00:00:00+00:00")
    assert len(late) == 1 and "后期" in late[0].text
