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


class TestCompress:
    @pytest.mark.asyncio
    async def test_sets_summary_and_rebuilds(self):
        llm = make_llm()
        structured = make_structured(full_summary())
        comp = make_compressor(llm=llm, structured=structured)
        messages = [
            Message(role="system", content="SKILL"),
            Message(role="system", content="MEMORY"),
            Message(role="system", content="旧摘要"),
            Message(role="user", content="q"),
            Message(role="assistant", content="a"),
        ]
        out = await comp.compress(messages)
        assert comp.summary is not None
        roles = [m.role for m in out]
        assert roles.count("system") == 3          # SKILL + MEMORY + 新摘要
        texts = [m.content for m in out if m.role == "system"]
        assert "SKILL" in texts and "MEMORY" in texts
        assert "旧摘要" not in texts               # 旧摘要被替换，绝不并存

    @pytest.mark.asyncio
    async def test_head_preserved_when_many_systems(self):
        llm = make_llm()
        structured = make_structured(full_summary())
        comp = make_compressor(llm=llm, structured=structured)
        messages = [
            Message(role="system", content="SKILL"),
            Message(role="system", content="MEMORY"),
            Message(role="system", content="旧摘要"),
            Message(role="user", content="q"),
        ]
        out = await comp.compress(messages)
        system_texts = [m.content for m in out if m.role == "system"]
        assert system_texts[0] == "SKILL"
        assert system_texts[1] == "MEMORY"

    @pytest.mark.asyncio
    async def test_fallback_when_extract_fails(self):
        llm = make_llm()
        structured = make_structured()    # 返回 fallback
        comp = make_compressor(llm=llm, structured=structured)
        messages = [Message(role="user", content="q")]
        out = await comp.compress(messages)
        assert comp.summary is not None


class TestSplitTailPairing:
    """成对约束补充测试（brief 未覆盖）：assistant(tool_calls) 与其 tool 结果不分离。"""

    @pytest.mark.asyncio
    async def test_tool_pulls_in_its_assistant_even_beyond_budget(self):
        # 预算极小（context_size=200 × reserve 0.1 = 20 token）：
        # assistant 本身远超预算，但 tool 结果被保留时其 assistant 必须被补回
        llm = make_llm()
        structured = make_structured(full_summary())
        comp = make_compressor(
            llm=llm, structured=structured,
            config=ContextConfig(context_size=200, reserve_ratio=0.1),
        )
        messages = [
            Message(role="user", content="q"),
            Message(
                role="assistant", content="z" * 500,
                tool_calls=[{
                    "id": "call_1", "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }],
            ),
            Message(role="tool", content="result", tool_call_id="call_1"),
        ]
        out = await comp.compress(messages)
        roles = [m.role for m in out]
        assert "tool" in roles
        idx = roles.index("assistant")
        assert roles[idx + 1] == "tool"            # 成对且顺序正确：assistant 在前
        assert out[idx].tool_calls[0]["id"] == "call_1"

    @pytest.mark.asyncio
    async def test_orphan_tool_dropped_when_assistant_missing(self):
        # tail 中出现 tool 消息但对应 assistant 不在（无任何 assistant(tool_calls)）
        # → 孤立 tool 消息必须被丢弃，否则 tool_call_id 无对应 → API 报错
        llm = make_llm()
        structured = make_structured(full_summary())
        comp = make_compressor(llm=llm, structured=structured)
        messages = [
            Message(role="user", content="q"),
            Message(role="tool", content="result", tool_call_id="call_x"),
        ]
        out = await comp.compress(messages)
        assert "tool" not in [m.role for m in out]
        assert comp.summary is not None
