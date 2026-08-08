import json
from pathlib import Path
from paperflow.core.memory.experience_memory import MemoryStore


def make_store(tmp_path, **kwargs):
    return MemoryStore(tmp_path, **kwargs)


class TestAppend:
    def test_append_returns_incrementing_cursor(self, tmp_path):
        store = make_store(tmp_path)
        assert store.append_history({"type": "tool", "tool_name": "echo"}) == 1
        assert store.append_history({"type": "tool", "tool_name": "echo"}) == 2

    def test_append_writes_jsonl_with_internal_fields(self, tmp_path):
        store = make_store(tmp_path)
        store.append_history({"type": "tool", "tool_name": "echo"})
        line = store.history_path.read_text().strip().splitlines()[0]
        record = json.loads(line)
        assert record["cursor"] == 1
        assert record["timestamp"]
        assert record["type"] == "tool"
        assert record["tool_name"] == "echo"

    def test_append_entry_cannot_override_cursor(self, tmp_path):
        store = make_store(tmp_path)
        store.append_history({"cursor": 999, "type": "tool"})
        record = json.loads(store.history_path.read_text().strip())
        assert record["cursor"] == 1     # 内部字段在后，绝不被 entry 覆盖


class TestReadUnprocessed:
    def test_reads_from_dream_cursor_by_default(self, tmp_path):
        store = make_store(tmp_path)
        for i in range(5):
            store.append_history({"type": "tool", "i": i})
        store.advance_dream_cursor(3)
        entries = store.read_unprocessed_history()
        assert [e["cursor"] for e in entries] == [4, 5]

    def test_reads_after_explicit_since(self, tmp_path):
        store = make_store(tmp_path)
        for i in range(5):
            store.append_history({"type": "tool", "i": i})
        entries = store.read_unprocessed_history(since=2)
        assert [e["cursor"] for e in entries] == [3, 4, 5]

    def test_limit(self, tmp_path):
        store = make_store(tmp_path)
        for i in range(10):
            store.append_history({"type": "tool", "i": i})
        entries = store.read_unprocessed_history(limit=3)
        assert len(entries) == 3

    def test_skips_corrupt_lines(self, tmp_path):
        store = make_store(tmp_path)
        store.append_history({"type": "tool", "i": 1})
        store.cursor_path.write_text("2", encoding="utf-8")   # 模拟崩溃 append：游标已推进、写入半途失败
        with open(store.history_path, "a") as f:
            f.write('{"cursor": 2, "type": "tool", "i": "half' + "\n")   # 半写坏行
        store.append_history({"type": "tool", "i": 3})
        entries = store.read_unprocessed_history()
        assert [e["cursor"] for e in entries] == [1, 3]


class TestCount:
    def test_count_zero_when_no_files(self, tmp_path):
        store = make_store(tmp_path)
        assert store.read_unprocessed_count() == 0

    def test_count_after_append(self, tmp_path):
        store = make_store(tmp_path)
        store.append_history({"type": "tool"})
        store.append_history({"type": "tool"})
        assert store.read_unprocessed_count() == 2

    def test_count_after_dream_cursor(self, tmp_path):
        store = make_store(tmp_path)
        store.append_history({"type": "tool"})
        store.append_history({"type": "tool"})
        store.advance_dream_cursor(1)
        assert store.read_unprocessed_count() == 1

    def test_count_survives_corrupt_cursor_file(self, tmp_path):
        store = make_store(tmp_path)
        store.append_history({"type": "tool"})
        store.cursor_path.write_text("not-a-number")
        assert store.read_unprocessed_count() == 0     # 防御：损坏按 0


class TestCompact:
    def test_keeps_recent_entries(self, tmp_path):
        store = make_store(tmp_path, max_history_entries=5)
        for i in range(10):
            store.append_history({"type": "tool", "i": i})
        store.advance_dream_cursor(5)     # 已处理 1..5，compact 应裁剪已处理头部
        store.compact_history()
        entries = store.read_unprocessed_history(since=0)
        assert [e["cursor"] for e in entries] == [6, 7, 8, 9, 10]

    def test_keeps_unprocessed_entries(self, tmp_path):
        store = make_store(tmp_path, max_history_entries=5)
        for i in range(10):
            store.append_history({"type": "tool", "i": i})
        store.advance_dream_cursor(7)     # 6,7,8,9,10 未处理（>7 是 8,9,10——游标语义）
        store.compact_history()
        entries = store.read_unprocessed_history(since=0)
        cursors = [e["cursor"] for e in entries]
        assert 8 in cursors and 9 in cursors and 10 in cursors
