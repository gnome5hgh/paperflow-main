# paperflow/core/intent/pipeline.py
"""意图识别四级级联编排。

四级级联（自顶向下逐级判定，前级未定夺才落到后级）：
- 实体提取：正则提取 PDF 路径/arXiv ID/DOI/Figure 等实体
- 追问检测：判断是否承接上一轮意图（依赖会话中的上一轮意图）
- 混合路由：命中非 general 直接产出；confidence 为融合分数 clip 到 [0,1]
  （cosine 可为负、稀疏点积可 >1，非概率）
- LLM 兜底：用结构化输出解析意图，注入路由近失候选供参考，改写缺省原文
"""
from pydantic import BaseModel

from paperflow.core.intent.schemas.intent import (
    IntentOutput, IntentType, IntentStep, IntentionResult,
)
from paperflow.core.intent.routing.entities import extract_entities
from paperflow.core.intent.routing.followup import detect_followup


class IntentPipeline:
    """意图识别四级级联编排：依赖混合路由器与结构化输出模块。"""

    def __init__(self, router, structured,
                 llm_fallback_schema: type[BaseModel] = IntentionResult):
        self.router = router
        self.structured = structured
        self.llm_fallback_schema = llm_fallback_schema

    async def run(self, query: str, prev_intent: IntentType | None = None,
                  prev_user_input: str = "") -> IntentOutput:
        """对一次用户输入做完整意图识别，返回结构化意图结果。

        :param query: 用户原始输入
        :param prev_intent: 上一轮意图（追问检测使用；首轮为 None）
        :param prev_user_input: 上轮原始输入（追问分支重跑实体提取用；首轮为空串）。
            上轮实体不存会话，用确定性正则重提取即可，零状态。
            调用方按三个参数调用，缺参会 TypeError。
        """

        # 实体提取（确定性正则，只提取不判定意图）
        entities = self._extract_entities(query)

        # 追问检测（词表启发式，依赖会话中的上一轮意图）
        if self._detect_followup(query, prev_intent):
            # 继承上轮意图；实体 = 上轮实体（从 prev_user_input 重跑实体提取）+ 本轮覆盖。
            # 合并顺序关键：上轮实体在前，本轮实体在后——同键（如 Figure）本轮赢
            prev_entities = extract_entities(prev_user_input) if prev_user_input else {}
            return IntentOutput(
                intent_type=prev_intent, confidence=1.0,
                entities={**prev_entities, **entities},
                source=IntentStep.FOLLOWUP, prev_intent=prev_intent,
                rewritten_query=query)

        # 混合路由：命中非 general 直接产出
        choice = self.router(query)
        if choice is not None and choice.name != "general":
            return IntentOutput(
                intent_type=IntentType(choice.name),
                # 融合分数 clip 到 [0,1]（cosine 可为负、稀疏点积可 >1，非概率）
                confidence=float(max(0.0, min(1.0, choice.similarity_score or 0.0))),
                entities=entities, source=IntentStep.ROUTER, prev_intent=prev_intent,
                rewritten_query=query)

        # LLM 兜底：注入路由近失候选供参考
        near_miss = self.router.scores(query, k=3)
        result = await self.structured.extract(
            prompt=self._build_llm_prompt(query, near_miss),
            schema=self.llm_fallback_schema,
            fallback=lambda: IntentionResult(intent_type=IntentType.GENERAL,
                                             confidence=0.0),
        )
        # steps/clarification 透传：复合意图拆分或澄清问题，由上层据此处理
        return IntentOutput(
            intent_type=result.intent_type,
            confidence=result.confidence,
            entities=entities, source=IntentStep.LLM, prev_intent=prev_intent,
            rewritten_query=result.query_rewrite or query,
            steps=result.steps or [],
            clarification=result.clarification,
        )

    def _extract_entities(self, query: str) -> dict:
        """实体提取（委托 entities.extract_entities；保留方法形态便于测试替换）。"""
        return extract_entities(query)

    def _detect_followup(self, query: str, prev_intent) -> bool:
        """追问检测（委托 followup.detect_followup；保留方法形态便于测试替换）。"""
        return detect_followup(query, prev_intent)

    def _build_llm_prompt(self, query: str, near_miss: list[tuple[str, float]]) -> str:
        """注入意图契约说明（IntentType 枚举）+ 路由近失候选（top 分数）
        + 原始 query。近失示例："路由层倾向 search_paper（0.31，差阈值一点），
        请确认或改判"。"""
        parts = [
            "你是意图分类器。从以下意图中选择一个：",
            ", ".join(t.value for t in IntentType),
            "输出 JSON：{intent_type, confidence, query_rewrite}。",
        ]
        if near_miss:
            parts.append("路由层近失候选（供参考，可确认或改判）：")
            for name, score in near_miss:
                parts.append(f"  - {name}: {score:.3f}")
        parts.append(f"用户输入：{query}")
        return "\n".join(parts)
