"""AuditMiddleware 并发写锁测试（Layer 4 parallel_spawn 并发就绪）。"""
import asyncio
import json
import threading
from datetime import datetime

from paperflow.core.security import ToolContext
from paperflow.core.security.audit import AuditMiddleware
from paperflow.core.tool import ToolResult


class _NoopTool:
    """最小工具替身：AuditMiddleware.after 只读 name / risk_level。"""
    name = "noop"
    risk_level = "low"


def test_audit_has_concurrency_lock(tmp_path):
    """回归锚点：无锁实现会 AttributeError（TDD 驱动失败的断言）。"""
    mw = AuditMiddleware(audit_dir=str(tmp_path))
    # CPython 中 threading.Lock 是工厂函数（allocate_lock）而非类型，
    # isinstance 第二参必须为真实类型对象 → 用新建锁的 type 作锚点
    assert isinstance(mw._lock, type(threading.Lock()))


def test_audit_concurrent_append_lines_intact(tmp_path):
    """行为护栏：16 线程并发写大 payload，逐行仍是合法 JSON（不交叉）。"""
    mw = AuditMiddleware(audit_dir=str(tmp_path))
    tool = _NoopTool()
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            ctx = ToolContext(
                trace_id=f"t{i}", session_id=f"s{i}", agent_type="test",
                tool=tool, tool_name="noop", timestamp="2026-08-03T00:00:00",
                started_at=0.0, args={"i": i, "big": "x" * 20000},
                result=ToolResult(text="ok"),
            )
            # after 是 async；worker 线程无 loop → asyncio.run 新建（与工具执行同形态）
            asyncio.run(mw.after(ctx))
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    day = datetime.now().strftime("%Y%m%d")
    lines = (tmp_path / f"audit_{day}.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    for line in lines:
        entry = json.loads(line)          # 每行合法 JSON = 无交叉
        assert entry["tool_name"] == "noop"
