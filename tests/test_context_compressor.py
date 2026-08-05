# tests/test_context_compressor.py
import pytest
from unittest.mock import MagicMock
from paperflow.core.llm import Message
from paperflow.core.memory.context_config import ContextConfig, SummarySchema
from paperflow.core.memory.context_compressor import ContextCompressor


def make_llm():
    llm = MagicMock()
    llm.context_window = 65536
    return llm


def make_structured(result: SummarySchema | None = None):
    structured = MagicMock()

    async def extract(prompt, schema, fallback=None):
        if result is not None:
            return result
        return fallback()

    structured.extract = extract
    return structured


def make_compressor(llm=None, structured=None, config=None):
    return ContextCompressor(
        config or ContextConfig(),
        llm or make_llm(),
        structured or make_structured(),
    )


def small_messages(n=10):
    return [Message(role="user", content="a" * 100) for _ in range(n)]


def full_summary() -> SummarySchema:
    return SummarySchema(
        task_overview="t", current_state="c",
        important_discoveries="d", next_steps="n", context_to_preserve="p",
    )


class TestShouldCompress:
    def test_no_compress_when_small(self):
        comp = make_compressor()
        assert comp.should_compress(small_messages(5)) is False

    def test_compress_when_huge(self):
        comp = make_compressor()
        huge = [Message(role="user", content="x" * 5000) for _ in range(100)]
        assert comp.should_compress(huge) is True


class TestSplitTailPairing:
    """成对约束补充测试（压缩后 tail 不分离 assistant(tool_calls) 与其 tool 结果）。"""

    @pytest.mark.asyncio
    async def test_tool_pulls_in_its_assistant_even_beyond_budget(self):
        llm = make_llm()
        structured = make_structured(full_summary())
        comp = make_compressor(
            llm=llm, structured=structured,
            config=ContextConfig(context_size=200, reserve_ratio=0.1),
        )
        comp.history = [
            Message(role="user", content="q"),
            Message(role="assistant", content="z" * 500,
                    tool_calls=[{"id": "call_1", "type": "function",
                                 "function": {"name": "f", "arguments": "{}"}}]),
            Message(role="tool", content="result", tool_call_id="call_1"),
        ]
        await comp.compress_history()
        roles = [m.role for m in comp.history]
        assert "tool" in roles
        idx = roles.index("assistant")
        assert roles[idx + 1] == "tool"                        # 成对且顺序正确
        assert comp.history[idx].tool_calls[0]["id"] == "call_1"

    @pytest.mark.asyncio
    async def test_multi_pairs_in_budget_no_duplicate_assistant(self):
        llm = make_llm()
        comp = make_compressor(llm=llm, structured=make_structured(full_summary()))
        comp.history = [Message(role="user", content="q")]
        for i in range(6):
            comp.history.append(Message(
                role="assistant", content=f"思考{i}",
                tool_calls=[{"id": f"call_{i}", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}],
            ))
            comp.history.append(Message(role="tool", content=f"结果{i}", tool_call_id=f"call_{i}"))
        tail = comp._split_tail(comp.history, ratio=comp.config.reserve_ratio)
        assistant_contents = [m.content for m in tail if m.role == "assistant"]
        for i in range(6):
            assert assistant_contents.count(f"思考{i}") == 1
        pair_seq = [m for m in tail if m.role in ("assistant", "tool")]
        for idx, m in enumerate(pair_seq):
            if m.role == "assistant":
                assert pair_seq[idx + 1].role == "tool"
                assert pair_seq[idx + 1].tool_call_id == m.tool_calls[0]["id"]

    @pytest.mark.asyncio
    async def test_orphan_tool_dropped_when_assistant_missing(self):
        llm = make_llm()
        structured = make_structured(full_summary())
        comp = make_compressor(llm=llm, structured=structured)
        comp.history = [
            Message(role="user", content="q"),
            Message(role="tool", content="result", tool_call_id="call_x"),
        ]
        await comp.compress_history()
        assert "tool" not in [m.role for m in comp.history]
        assert comp.history[0].role == "system"               # 压缩仍生成摘要


class TestHistoryAccumulate:
    def test_accumulate_appends_conv(self):
        comp = make_compressor()
        comp.accumulate([
            Message(role="user", content="q"),
            Message(role="assistant", content="a"),
        ])
        assert [m.content for m in comp.history] == ["q", "a"]

    def test_accumulate_dedup_tail_window(self):
        # 防御性双算：conv 与 history 尾有同对象时不重复追加
        comp = make_compressor()
        comp.accumulate([Message(role="user", content="q")])
        m = comp.history[-1]                      # 取 history 里的同对象
        comp.accumulate([m, Message(role="assistant", content="a")])
        assert [x.content for x in comp.history] == ["q", "a"]

    def test_accumulate_skips_system(self):
        # 只存对话消息，system（SKILL/摘要）不进累积
        comp = make_compressor()
        comp.accumulate([Message(role="system", content="SKILL"),
                         Message(role="user", content="q")])
        assert [m.role for m in comp.history] == ["user"]

    def test_summary_text_none_when_no_system_head(self):
        comp = make_compressor()
        comp.accumulate([Message(role="user", content="q")])
        assert comp._summary_text() is None

    def test_summary_text_when_system_head(self):
        comp = make_compressor()
        comp.history = [Message(role="system", content="摘要"),
                        Message(role="user", content="q")]
        assert comp._summary_text() == "摘要"


class TestCompressHistory:
    @pytest.mark.asyncio
    async def test_rewrites_history_in_place(self):
        llm = make_llm()
        comp = make_compressor(llm=llm, structured=make_structured(full_summary()))
        comp.history = [
            Message(role="user", content="q1"),
            Message(role="assistant", content="a1"),
            Message(role="user", content="q2"),
            Message(role="assistant", content="a2"),
        ]
        await comp.compress_history()
        assert comp.history[0].role == "system"
        assert comp.history[0].content.startswith("[对话摘要]")
        # 近对话 tail 保留（小消息远低于 reserve_ratio×32K → 全部保留）
        assert [m.role for m in comp.history[1:]] == ["user", "assistant", "user", "assistant"]

    @pytest.mark.asyncio
    async def test_incremental_uses_old_summary(self):
        # 已有摘要消息时，_build_compression_prompt 把旧摘要作为增量输入
        llm = make_llm()
        seen_prompts = []
        structured = MagicMock()
        async def extract(prompt, schema, fallback=None):
            seen_prompts.append(prompt)
            return full_summary()
        structured.extract = extract
        comp = make_compressor(llm=llm, structured=structured)
        comp.history = [Message(role="system", content="旧摘要"),
                        Message(role="user", content="q")]
        await comp.compress_history()
        assert "旧摘要" in seen_prompts[0]                    # 增量输入
        assert "对话内容：" in seen_prompts[0]
        assert "SKILL" not in seen_prompts[0]                 # system 不进压缩输入

    @pytest.mark.asyncio
    async def test_tail_pairing_no_orphan_tool(self):
        llm = make_llm()
        comp = make_compressor(
            llm=llm, structured=make_structured(full_summary()),
            config=ContextConfig(context_size=200, reserve_ratio=0.1),
        )
        comp.history = [
            Message(role="user", content="q"),
            Message(role="assistant", content="z" * 500,
                    tool_calls=[{"id": "call_1", "type": "function",
                                 "function": {"name": "f", "arguments": "{}"}}]),
            Message(role="tool", content="result", tool_call_id="call_1"),
            Message(role="user", content="q2"),
        ]
        await comp.compress_history()
        tool_ids = [m.tool_call_id for m in comp.history if m.role == "tool"]
        ass_ids = {tc["id"] for m in comp.history
                   if m.role == "assistant" and m.tool_calls
                   for tc in m.tool_calls}
        assert all(tid in ass_ids for tid in tool_ids)        # 无孤立 tool

    @pytest.mark.asyncio
    async def test_failure_keeps_raw_history(self):
        llm = make_llm()
        structured = MagicMock()
        async def extract(prompt, schema, fallback=None):
            raise RuntimeError("boom")
        structured.extract = extract
        comp = make_compressor(llm=llm, structured=structured)
        comp.history = [Message(role="user", content="q")]
        await comp.compress_history()
        assert [m.content for m in comp.history] == ["q"]     # 原样保留，不丢对话
