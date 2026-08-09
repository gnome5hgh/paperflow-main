"""Compaction 测试：触发判定 + sliding_window 摘要进 index 1 + 原始消息保留。"""
import asyncio

from paperflow.core.llm import Message as WireMessage
from paperflow.core.memory.compaction import (
    CompactionSettings, should_compress, run_compaction)


def _run(awaitable):
    return asyncio.get_event_loop().run_until_complete(awaitable)


def _tool_call(call_id: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "f", "arguments": "{}"}}


def test_resolve_context_size():
    s = CompactionSettings()
    assert s.resolve_context_size(1000000) == 500000
    s2 = CompactionSettings(context_size=10000)
    assert s2.resolve_context_size(1000000) == 10000


def test_should_compress_threshold():
    # 阈值：estimate×1.1 > trigger_ratio×ctx_size(=0.5×100=50)。
    # 注意 cl100k 会把重复字符合并成多字节 token（"x"*200 仅 ~25 token），
    # 必须用足够大的内容才能真实跨过阈值——原 brief 用 "x"*200 是假失败。
    s = CompactionSettings(trigger_ratio=0.5, context_size=100)
    big = [WireMessage(role="user", content="x" * 1000)]   # ~125 token → 141 > 50
    small = [WireMessage(role="user", content="hi")]
    assert should_compress(big, s, 200) is True
    assert should_compress(small, s, 200) is False


def test_sliding_window_evicts_old_inserts_summary():
    s = CompactionSettings(mode="sliding_window", context_size=1000,
                           reserve_ratio=0.2)
    messages = [
        WireMessage(role="system", content="身份"),
        WireMessage(role="user", content="任务"),
        WireMessage(role="assistant", content="回答"),
    ]

    class FakeStructured:
        async def extract(self, prompt, schema, fallback=None):
            return schema(task_overview="概述", current_state="状态",
                          important_discoveries="发现", next_steps="下一步",
                          context_to_preserve="保留")
    out = _run(
        run_compaction(messages, s, llm=None,
                       structured=FakeStructured(),
                       summary_text="[摘要] 概述 / 状态 / 发现 / 下一步 / 保留"))
    assert out[0].role == "system"          # 头部身份保留
    assert out[1].role == "system"          # index 1 摘要
    assert "概述" in out[1].content
    # sliding_window 保留近期对话尾部（reserve_ratio 预算内）
    assert any(m.content in ("回答", "任务") for m in out)


def test_summarize_via_structured_output():
    """summary_text 未传时走 StructuredOutput + SummarySchema 生成摘要（all_messages 全量压）。"""
    s = CompactionSettings(mode="all_messages", context_size=1000,
                           reserve_ratio=0.2)
    messages = [
        WireMessage(role="system", content="身份"),
        WireMessage(role="user", content="任务"),
        WireMessage(role="assistant", content="回答"),
    ]

    class FakeStructured:
        async def extract(self, prompt, schema, fallback=None):
            assert schema.__name__ == "SummarySchema"
            return schema(task_overview="概述", current_state="状态",
                          important_discoveries="发现", next_steps="下一步",
                          context_to_preserve="保留")
    out = _run(run_compaction(messages, s, llm=None,
                              structured=FakeStructured()))
    assert out[0].role == "system" and out[0].content == "身份"
    assert out[1].role == "system"
    assert out[1].content.startswith("[对话摘要]")
    assert "任务：概述" in out[1].content
    assert len(out) == 2            # all_messages：全量压成摘要，无近期尾部


def test_sliding_window_keeps_tool_pair_together():
    """成对约束：tail 里的 tool 与其 assistant(tool_calls) 同时保留且顺序正确。"""
    s = CompactionSettings(mode="sliding_window", context_size=1000,
                           reserve_ratio=0.2)
    messages = [
        WireMessage(role="system", content="身份"),
        WireMessage(role="user", content="任务"),
        WireMessage(role="assistant", content="调用", tool_calls=[_tool_call("call_1")]),
        WireMessage(role="tool", content="结果", tool_call_id="call_1"),
        WireMessage(role="user", content="追问"),
        WireMessage(role="assistant", content="回答"),
    ]
    out = _run(run_compaction(messages, s, llm=None, structured=None,
                              summary_text="摘要"))
    roles = [m.role for m in out]
    assert roles.count("tool") == 1
    ti = roles.index("tool")
    assert roles[ti - 1] == "assistant"
    assert out[ti - 1].tool_calls[0]["id"] == "call_1"
    assert out[ti].tool_call_id == "call_1"


def test_orphan_tool_dropped():
    """绝不允许孤立 tool 消息：tool_call_id 无对应 assistant 携带时整条清除。"""
    s = CompactionSettings(mode="sliding_window", context_size=1000,
                           reserve_ratio=0.2)
    messages = [
        WireMessage(role="system", content="身份"),
        WireMessage(role="user", content="问题"),
        WireMessage(role="assistant", content="思考", tool_calls=[_tool_call("call_a")]),
        # 异常轨迹：tool 引用的 call_x 在向前没有任何 assistant 携带该 id
        WireMessage(role="tool", content="结果x", tool_call_id="call_x"),
        WireMessage(role="user", content="继续"),
    ]
    out = _run(run_compaction(messages, s, llm=None, structured=None,
                              summary_text="摘要"))
    assert all(m.tool_call_id not in ("call_x",) for m in out if m.role == "tool")
    # 摘要与头部仍保留
    assert out[0].role == "system" and out[1].role == "system"
