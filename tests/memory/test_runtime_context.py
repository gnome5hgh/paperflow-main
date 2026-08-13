"""runtime_context：未绑定返回 None；set 后 get 返回同一上下文。"""
import pytest

from paperflow.core.memory.tools.runtime_context import (
    MemoryToolsContext, set_memory_context, get_memory_context)


@pytest.fixture(autouse=True)
def _reset_ctx():
    yield
    set_memory_context(None)


def test_unbound_returns_none():
    set_memory_context(None)
    assert get_memory_context() is None


def test_set_then_get():
    ctx = MemoryToolsContext(agent_id="sess_1")
    set_memory_context(ctx)
    assert get_memory_context() is ctx
    set_memory_context(None)   # 清理，防污染其它测试
