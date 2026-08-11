import pytest
from unittest.mock import MagicMock
from paperflow.core.intent.schema import Route, RouteChoice
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.intent_schema import (
    IntentOutput, IntentType, IntentStep, IntentionResult,
)
from paperflow.core.intent.hybrid_router import HybridRouter
from paperflow.core.intent.dense_encoder import FixedDenseEncoder
from paperflow.core.intent.entities import extract_entities
from paperflow.core.intent.followup_detector import detect_followup


class _MissRouter(HybridRouter):
    """对预设"无关查询"显式返回 miss（None）的测试替身。

    为什么需要：FixedDenseEncoder + BM25 的融合分数对任意中文文本都偏高
    （稀疏点积主导，top 路由分 0.86~1.10），真实 router 在无阈值配置下
    对任何输入都返回命中——Stage 3 LLM 兜底路径无法用真实 router 触发。
    子类只把 brief 中标注"应走 LLM 兜底"的查询短路为 None（等价真实场景
    融合分低于阈值的路由 miss），其余行为完全委托父类，保留 scores() 近失
    候选的真实性（test_prompt_includes_near_miss 依赖 route 名）。
    """

    miss_queries = frozenset({
        "帮我写个笔记吧",
        "完全无关的文本内容测试一下",
        "完全无关的随机文本",
    })

    def __call__(self, text=None, vector=None, sparse_vector=None,
                 simulate_static=False):
        if text in self.miss_queries:
            return None
        return super().__call__(text=text, vector=vector,
                                sparse_vector=sparse_vector,
                                simulate_static=simulate_static)


def make_router(routes=None):
    routes = routes or [
        Route(name="search_paper", utterances=["下载最新论文", "搜索 circRNA 文献"]),
        Route(name="generate_note", utterances=["把这篇论文整理成笔记", "写一份笔记"]),
        Route(name="ask_question", utterances=["circRNA 的机制是什么", "解释一下这个公式"]),
    ]
    return _MissRouter(encoder=FixedDenseEncoder(dim=64), routes=routes)


def make_structured(result=None):
    structured = MagicMock()
    async def extract(prompt, schema, fallback=None):
        if result is not None:
            return result
        return fallback()
    structured.extract = extract
    return structured


class TestPipeline:
    @pytest.mark.asyncio
    async def test_router_hit(self):
        pipe = IntentPipeline(router=make_router(),
                              structured=make_structured())
        out = await pipe.run("下载最新论文")
        assert out.source == IntentStep.ROUTER
        assert out.intent_type == IntentType.SEARCH_PAPER
        assert out.rewritten_query == "下载最新论文"      # ROUTER 分支 = 原文
        assert 0.0 <= out.confidence <= 1.0              # clip 生效

    @pytest.mark.asyncio
    async def test_router_miss_falls_to_llm(self):
        result = IntentionResult(intent_type=IntentType.GENERATE_NOTE,
                                 confidence=0.8, query_rewrite="写笔记")
        pipe = IntentPipeline(router=make_router(),
                              structured=make_structured(result))
        out = await pipe.run("帮我写个笔记吧")
        assert out.source == IntentStep.LLM
        assert out.intent_type == IntentType.GENERATE_NOTE
        assert out.rewritten_query == "写笔记"           # LLM 改写透传

    @pytest.mark.asyncio
    async def test_llm_fallback_when_extract_fails(self):
        pipe = IntentPipeline(router=make_router(),
                              structured=make_structured())   # 返回 fallback
        out = await pipe.run("完全无关的文本内容测试一下")
        assert out.intent_type == IntentType.GENERAL
        assert out.source == IntentStep.LLM
        assert out.confidence == 0.0
        assert out.rewritten_query == "完全无关的文本内容测试一下"   # 缺省原文

    @pytest.mark.asyncio
    async def test_stage01_stubs(self):
        pipe = IntentPipeline(router=make_router(),
                              structured=make_structured())
        assert pipe._extract_entities("下载 paper/pdf/x.pdf") == {}
        assert pipe._detect_followup("那这篇呢", IntentType.SEARCH_PAPER) is False

    @pytest.mark.asyncio
    async def test_prompt_includes_near_miss(self):
        captured = {}
        structured = MagicMock()
        async def extract(prompt, schema, fallback=None):
            captured["prompt"] = prompt
            return fallback()
        structured.extract = extract
        pipe = IntentPipeline(router=make_router(), structured=structured)
        await pipe.run("完全无关的随机文本")
        assert "search_paper" in captured["prompt"] or "路由" in captured["prompt"]


class TestPipelineLayer4Wiring:
    @pytest.mark.asyncio
    async def test_followup_inherits_prev_intent_with_merged_entities(self):
        pipe = IntentPipeline(router=make_router(), structured=make_structured())
        out = await pipe.run("那 Figure 3 呢？", prev_intent=IntentType.ASK_QUESTION,
                             prev_user_input="/Users/me/paper.pdf 讲了什么")
        assert out.source == IntentStep.FOLLOWUP
        assert out.intent_type == IntentType.ASK_QUESTION
        assert out.entities["pdf_path"] == "/Users/me/paper.pdf"   # 上轮实体继承
        assert out.entities["figure"] == "3"                        # 本轮覆盖/新增

    @pytest.mark.asyncio
    async def test_stage3_passes_steps_and_clarification(self):
        result = IntentionResult(intent_type=IntentType.GENERAL, confidence=0.4,
                                 steps=[IntentType.SEARCH_PAPER, IntentType.GENERATE_NOTE],
                                 clarification="要搜索还是生成笔记？")
        pipe = IntentPipeline(router=make_router(), structured=make_structured(result))
        out = await pipe.run("帮我写个笔记吧")     # miss 查询 → Stage 3
        assert out.steps == [IntentType.SEARCH_PAPER, IntentType.GENERATE_NOTE]
        assert out.clarification == "要搜索还是生成笔记？"

    @pytest.mark.asyncio
    async def test_real_extract_entities_delegation(self):
        """方法形态委托：既兼容 stub 断言，又是真实实现。"""
        pipe = IntentPipeline(router=make_router(), structured=make_structured())
        assert pipe._extract_entities("/abs/x.pdf") == {"pdf_path": "/abs/x.pdf"}
        assert pipe._detect_followup("那第三篇呢", IntentType.SEARCH_PAPER) is True
