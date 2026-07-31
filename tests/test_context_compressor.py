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

    @pytest.mark.asyncio
    async def test_fallback_preserves_conversation_not_instructions(self):
        # fallback 的 context_to_preserve 必须是对话内容，不能以压缩指令开头
        llm = make_llm()
        structured = make_structured()    # 返回 fallback
        comp = make_compressor(llm=llm, structured=structured)
        messages = [Message(role="user", content="请记住我偏好 X")]
        await comp.compress(messages)
        assert "你是对话上下文压缩器" not in comp.summary     # 指令未混入
        assert "请记住我偏好 X" in comp.summary               # 对话内容被保留

    @pytest.mark.asyncio
    async def test_no_memory_md_old_summary_not_coexisting(self):
        # 无 MEMORY.md：messages 只有 SKILL + 旧摘要。压缩后头部 system
        # 必须是 [SKILL, 新摘要]，旧摘要绝不并存（此前占 slot 1 存活）
        llm = make_llm()
        structured = MagicMock()
        async def extract(prompt, schema, fallback=None):
            if "q1" in prompt:
                return full_summary()                       # 第一轮：旧摘要
            return SummarySchema(                           # 第二轮：新摘要（不同文本）
                task_overview="t2", current_state="c2",
                important_discoveries="d2", next_steps="n2", context_to_preserve="p2",
            )
        structured.extract = extract
        comp = make_compressor(llm=llm, structured=structured)
        await comp.compress([
            Message(role="system", content="SKILL"),
            Message(role="user", content="q1"),
        ])
        old_summary = comp.summary
        assert old_summary is not None
        out2 = await comp.compress([
            Message(role="system", content="SKILL"),
            Message(role="system", content=old_summary),   # 无 MEMORY → 旧摘要占 slot 1
            Message(role="user", content="q2"),
        ])
        system_texts = [m.content for m in out2 if m.role == "system"]
        assert system_texts[0] == "SKILL"
        assert old_summary not in system_texts             # 旧摘要被排除
        assert system_texts[1].startswith("[对话摘要]")
        assert "任务：t2" in system_texts[1]               # 新摘要存活
        assert len(system_texts) == 2                       # 只有 SKILL + 新摘要


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
    async def test_multi_pairs_in_budget_no_duplicate_assistant(self):
        # 多对 assistant(tool_calls)+tool 全在预算内：配对循环补回 assistant，
        # 主循环再次遇到同一 assistant 时必须跳过，不得重复追加
        llm = make_llm()
        comp = make_compressor(llm=llm, structured=make_structured(full_summary()))
        messages = [Message(role="user", content="q")]
        for i in range(6):
            messages.append(Message(
                role="assistant", content=f"思考{i}",
                tool_calls=[{
                    "id": f"call_{i}", "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }],
            ))
            messages.append(Message(role="tool", content=f"结果{i}", tool_call_id=f"call_{i}"))
        tail = comp._split_tail(messages, ratio=comp.config.reserve_ratio)
        assistant_contents = [m.content for m in tail if m.role == "assistant"]
        for i in range(6):
            assert assistant_contents.count(f"思考{i}") == 1    # 每个 assistant 恰好一次
        # 成对且顺序正确：每个 assistant(tool_calls) 之后紧跟其 tool 结果
        pair_seq = [m for m in tail if m.role in ("assistant", "tool")]
        for idx, m in enumerate(pair_seq):
            if m.role == "assistant":
                assert pair_seq[idx + 1].role == "tool"
                assert pair_seq[idx + 1].tool_call_id == m.tool_calls[0]["id"]

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
