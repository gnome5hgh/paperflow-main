# tests/test_dream.py
import time

import pytest
from unittest.mock import MagicMock
from paperflow.core.memory.dream import (
    Dream, DreamEdit, DreamEditBatch, DREAM_CONSUMABLE_TYPES,
)
from paperflow.core.memory.experience_memory import MemoryStore
from paperflow.core.memory.gitstore import GitStore


def make_dream(tmp_path, structured=None, min_interval_s=0.0, llm=None):
    store = MemoryStore(tmp_path / "memory")
    git = GitStore(tmp_path / "memory")
    if structured is None:
        structured = MagicMock()
        async def extract(prompt, schema, fallback=None):
            return fallback()
        # 用 side_effect 而不是直接赋值，保留 Mock 以便 assert_not_called
        structured.extract.side_effect = extract
    llm = llm or MagicMock()
    return Dream(store, git, llm, structured=structured, min_interval_s=min_interval_s)


class TestConsumableTypes:
    def test_structured_output_excluded(self):
        assert "structured_output" not in DREAM_CONSUMABLE_TYPES
        assert "tool" in DREAM_CONSUMABLE_TYPES


class TestRunOnceIfDue:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_nothing_unprocessed(self, tmp_path):
        dream = make_dream(tmp_path)
        # 无历史 → 不触发 LLM
        await dream.run_once_if_due()
        dream.structured.extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_respects_min_interval(self, tmp_path):
        store = MemoryStore(tmp_path / "memory")
        store.append_history({"type": "tool", "tool_name": "echo"})
        dream = Dream(store, GitStore(tmp_path / "memory"), MagicMock(),
                      structured=MagicMock(), min_interval_s=3600.0)
        await dream.run_once_if_due()
        dream.structured.extract.assert_not_called()   # 间隔未到

    @pytest.mark.asyncio
    async def test_interval_between_consecutive_runs(self, tmp_path):
        dream = make_dream(tmp_path)
        dream.min_interval_s = 3600.0
        dream.store.append_history({"type": "tool", "tool_name": "echo"})
        dream._last_run = time.monotonic() - 7200.0   # 上次运行在 2h 前 → 该跑
        await dream.run_once_if_due()
        assert dream.structured.extract.call_count == 1
        # 新历史到达后再检查——否则游标已推进、短路返回，测不到间隔门控本身
        dream.store.append_history({"type": "tool", "tool_name": "echo"})
        await dream.run_once_if_due()                 # 刚跑完 → 应被间隔挡住
        assert dream.structured.extract.call_count == 1


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_edits_applied_and_committed(self, tmp_path):
        dream = make_dream(tmp_path)
        dream.store.append_history({"type": "tool", "tool_name": "echo", "summary": {}})
        async def extract(prompt, schema, fallback=None):
            return DreamEditBatch(edits=[DreamEdit(
                file="user_role.md", action="append",
                content="用户研究生物信息学", hook="用户画像",
            )])
        dream.structured.extract = extract

        await dream.run_once_if_due()

        memory_dir = tmp_path / "memory"
        assert (memory_dir / "user_role.md").exists()
        assert "生物信息学" in (memory_dir / "user_role.md").read_text()
        # MEMORY.md 索引同步
        assert "user_role" in (memory_dir / "MEMORY.md").read_text()
        # 游标推进
        assert dream.store.read_unprocessed_count() == 0

    @pytest.mark.asyncio
    async def test_invalid_edit_file_rejected(self, tmp_path):
        dream = make_dream(tmp_path)
        dream.store.append_history({"type": "tool", "tool_name": "echo"})
        async def extract(prompt, schema, fallback=None):
            return DreamEditBatch(edits=[DreamEdit(
                file="../../etc/crontab", action="append", content="* * * * * x",
            )])
        dream.structured.extract = extract

        await dream.run_once_if_due()
        # ../../etc/crontab 从 tmp_path/memory 解析 → tmp_path/etc/crontab（未写入）
        assert not (tmp_path / "etc" / "crontab").exists()
        # 失败计数 +1（首次不强制前进）
        assert dream._failures == 1

    @pytest.mark.asyncio
    async def test_delete_memory_md_rejected(self, tmp_path):
        dream = make_dream(tmp_path)
        dream.store.append_history({"type": "tool", "tool_name": "echo"})
        async def extract(prompt, schema, fallback=None):
            return DreamEditBatch(edits=[DreamEdit(
                file="MEMORY.md", action="delete", content="",
            )])
        dream.structured.extract = extract
        await dream.run_once_if_due()
        assert dream._failures == 1

    @pytest.mark.asyncio
    async def test_two_phase_atomicity_no_partial_writes(self, tmp_path):
        dream = make_dream(tmp_path)
        dream.store.append_history({"type": "tool", "tool_name": "echo"})
        async def extract(prompt, schema, fallback=None):
            return DreamEditBatch(edits=[
                DreamEdit(file="user_role.md", action="append", content="A", hook="h"),
                DreamEdit(file="../../evil.md", action="append", content="B", hook="h"),
            ])
        dream.structured.extract = extract
        await dream.run_once_if_due()
        # 阶段 1 预验证失败 → 一条都不写（无部分应用）
        assert not (tmp_path / "memory" / "user_role.md").exists()

    @pytest.mark.asyncio
    async def test_pure_monitoring_records_advance_cursor(self, tmp_path):
        dream = make_dream(tmp_path)
        dream.store.append_history({"type": "structured_output", "schema": "X"})
        await dream.run_once_if_due()
        assert dream.store.read_unprocessed_count() == 0    # 游标已推进
        dream.structured.extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_three_times_forces_cursor(self, tmp_path):
        dream = make_dream(tmp_path)
        dream.store.append_history({"type": "tool", "tool_name": "echo"})
        async def extract(prompt, schema, fallback=None):
            return DreamEditBatch(edits=[DreamEdit(
                file="user_role.md", action="replace", content="x", hook="h",
            )])
        dream.structured.extract = extract
        # 连续 3 次失败（git.commit 抛异常）→ 强制推进游标
        dream.git.commit = MagicMock(side_effect=Exception("disk full"))

        for _ in range(3):
            await dream.run_once_if_due()
        assert dream.store.read_unprocessed_count() == 0    # 连败 3 次强制前进


class TestCursorAdvance:
    @pytest.mark.asyncio
    async def test_more_than_max_entries_consumes_limit_keeps_rest(self, tmp_path):
        # 25 条 pending > max_entries=20 → 本轮只消费 20 条，
        # 游标推进到最后消费条目的 cursor（第 20 条），剩余 5 条下次再 Dream
        dream = make_dream(tmp_path)
        dream.max_entries = 20
        for i in range(25):
            dream.store.append_history({"type": "tool", "tool_name": f"echo{i}"})
        await dream.run_once_if_due()
        assert dream.store.read_unprocessed_count() == 5       # 第 21-25 条未跳过
        await dream.run_once_if_due()                          # 下一轮消费剩余 5 条
        assert dream.store.read_unprocessed_count() == 0

    @pytest.mark.asyncio
    async def test_bad_line_tail_cursor_never_regresses(self, tmp_path):
        # 已处理 3 条后出现半写坏行（.cursor=4 但记录未写完）：
        # 游标推进到单调最大值，绝不回退 → 已处理条目不被重新 Dream
        dream = make_dream(tmp_path)
        for i in range(3):
            dream.store.append_history({"type": "tool", "tool_name": f"echo{i}"})
        await dream.run_once_if_due()
        assert dream.store.read_unprocessed_count() == 0
        # 模拟崩溃残留：游标已推进到 4，但 history 只有半写坏行
        with open(dream.store.history_path, "a", encoding="utf-8") as f:
            f.write('{"type": "tool", "tool_nam')
        dream.store._write_cursor(4)
        assert dream.store.read_unprocessed_count() == 1       # 触发一次 run
        await dream.run_once_if_due()
        assert dream.store.read_unprocessed_count() == 0       # 不回退（旧实现回退到 0 → 重新处理 3 条）
        assert dream.store._read_dream_cursor() == 4           # 停在单调最大值
        assert dream.structured.extract.call_count == 1        # 坏行轮不触发 LLM


class TestApplyEdit:
    @pytest.mark.asyncio
    async def test_append_syncs_index(self, tmp_path):
        dream = make_dream(tmp_path)
        dream._apply_edit(DreamEdit(file="feedback_testing.md", action="append",
                                    content="规则", hook="测试反馈"))
        idx = (tmp_path / "memory" / "MEMORY.md").read_text()
        assert "feedback_testing" in idx

    @pytest.mark.asyncio
    async def test_delete_removes_index_line(self, tmp_path):
        dream = make_dream(tmp_path)
        dream._apply_edit(DreamEdit(file="user_role.md", action="append",
                                    content="x", hook="角色"))
        dream._apply_edit(DreamEdit(file="user_role.md", action="delete",
                                    content="", hook=""))
        idx = (tmp_path / "memory" / "MEMORY.md").read_text()
        assert "user_role" not in idx
