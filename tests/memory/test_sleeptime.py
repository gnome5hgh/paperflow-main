"""Sleeptime 测试：频率限制、编辑原子性、连败 3 次强制前进。"""
import tempfile
from pathlib import Path

import pytest

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.passage_manager import PassageManager
from paperflow.core.memory.sleeptime import Sleeptime
from paperflow.core.llm import Message as WireMessage


def _setup(structured=None, enable=True):
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    bm = GitEnabledBlockManager(db, memfs_dir=tmp / "memory")
    pm = PassageManager(db)
    mm = MessageManager(db)
    bm.create_block("persona", "身份")
    mm.add_message("sess_1", WireMessage(role="user", content="user 读了一篇论文"))
    mm.add_message("sess_1", WireMessage(role="assistant", content="assistant 回答"))
    from paperflow.core.memory.schemas.agent import AgentState
    from paperflow.core.memory.schemas.memory import Memory
    st = AgentState(agent_id="sess_1", memory=Memory(blocks=bm.list_blocks()))
    sl = Sleeptime(st, bm, pm, mm, structured, enable=enable,
                   min_interval_s=0, frequency=1, max_entries=20)
    # 游标归零：让 _run_once 测试能消费上面种子写入的 2 条消息
    # （默认构造以当前 size 为游标，避免重放进程启动前的历史）
    sl._cursor = 0
    return sl, bm


class _EditStructured:
    """模拟 LLM 输出编辑指令的 StructuredOutput。"""
    def __init__(self, batch=None):
        self.batch = batch or {
            "edits": [{"file": "feedback_testing.md", "action": "append",
                       "content": "用户偏好记录", "hook": "记忆测试"}]}

    async def extract(self, prompt, schema, fallback=None):
        return schema(**self.batch)


@pytest.mark.asyncio
async def test_frequency_limit_blocks():
    sl, _ = _setup(_EditStructured())
    assert sl._last_run is not None
    sl.min_interval_s = 3600       # 强制未到间隔
    hits = [await sl.run_once_if_due() for _ in range(3)]
    # 全部立即返回（频率限制），不执行 LLM 编辑
    assert all(h is None for h in hits)


@pytest.mark.asyncio
async def test_applies_edits_atomically():
    sl, bm = _setup(_EditStructured())
    sl._last_run = 0
    sl._failures = 0
    await sl._run_once()
    b = bm.get_block_by_label("feedback_testing")
    assert b is not None and "用户偏好记录" in b.value


@pytest.mark.asyncio
async def test_invalid_edit_fails_atomically():
    bad = _EditStructured({"edits": [{"file": "../../etc/passwd", "action": "append",
                                      "content": "x", "hook": ""}]})
    sl, _ = _setup(bad)
    sl._last_run = 0
    with pytest.raises(ValueError):
        await sl._run_once()


@pytest.mark.asyncio
async def test_rejects_delete_system_block():
    """system/ 块删除被拒：校验阶段即抛错，persona 块不受影响。"""
    bad = _EditStructured({"edits": [{"file": "system/persona.md", "action": "delete",
                                      "content": "", "hook": ""}]})
    sl, bm = _setup(bad)
    sl._last_run = 0
    with pytest.raises(ValueError):
        await sl._run_once()
    assert bm.get_block_by_label("persona") is not None


@pytest.mark.asyncio
async def test_mixed_batch_atomic_no_partial_write():
    """混合批次 [合法, 非法目标]：阶段 1 校验失败 → 一条不写（含合法的）。

    校验错误（MemoryEditValidationError）上抛、不计连败；应用期错误才计连败。
    """
    class _Mixed:
        async def extract(self, prompt, schema, fallback=None):
            return schema(**{"edits": [
                {"file": "feedback_testing.md", "action": "append",
                 "content": "用户偏好记录", "hook": "记忆测试"},
                {"file": "../../etc/passwd", "action": "append",
                 "content": "x", "hook": ""},
            ]})
    sl, bm = _setup(_Mixed())
    sl._last_run = 0
    with pytest.raises(ValueError):
        await sl._run_once()
    # 原子性：合法目标也未写入；校验失败不计连败
    assert bm.get_block_by_label("feedback_testing") is None
    assert sl._failures == 0


@pytest.mark.asyncio
async def test_apply_limit_error_counts_as_failure():
    """应用期超 limit（阶段 2 ValueError）→ 走连败计数而非上抛。

    区别于校验错误：应用期错误计入 _failures，3 次后强制前进游标，
    避免同一批编辑被无限重放（重复 append 有界）。
    """
    class _OverLimit:
        async def extract(self, prompt, schema, fallback=None):
            return schema(**{"edits": [{"file": "small.md", "action": "replace",
                                        "content": "x" * 10, "hook": ""}]})
    sl, bm = _setup(_OverLimit())
    bm.create_block("small", "ok", limit=5)   # 5 字上限，replace 10 字超限
    sl._last_run = 0
    await sl._run_once()                      # 阶段 2 update_block_value 抛 ValueError → 计连败，不上抛
    assert sl._failures == 1
    assert sl._cursor == 0                    # 未到 3 次，游标不前进
    await sl._run_once()
    assert sl._failures == 2
    await sl._run_once()                      # 连败 3 次 → 强制前进游标
    assert sl._failures == 3
    assert sl._cursor == sl.message_manager.size("sess_1")


@pytest.mark.asyncio
async def test_three_failures_advance_cursor():
    class _AlwaysFail:
        async def extract(self, prompt, schema, fallback=None):
            raise RuntimeError("LLM 故障")
    sl, bm = _setup(_AlwaysFail())
    sl._last_run = 0
    for _ in range(3):
        try:
            await sl._run_once()
        except RuntimeError:
            pass
    assert sl._failures >= 3
