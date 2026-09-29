# paperflow/core/intent/pipeline.py
"""意图识别五级级联编排。

五级级联（自顶向下逐级判定，前级未定夺才落到后级）：
- 实体提取：正则提取 PDF 路径/arXiv ID/DOI/Figure 等实体
- 选项答复检测：纯编号菜单选择直接产出 MENU_SELECTION（确定性正则，不重分类）
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
from paperflow.core.intent.routing.option_reply import is_option_reply


class IntentPipeline:
    """意图识别五级级联编排：依赖混合路由器与结构化输出模块。"""

    def __init__(self, router, structured,
                 llm_fallback_schema: type[BaseModel] = IntentionResult):
        """初始化意图处理管线。

        Args:
            router: HybridRouter 实例（routing/router.py），用于混合路由判定。
            structured: 结构化输出模块（如 LLM 调用封装），提供 extract 异步方法。
            llm_fallback_schema: LLM 兜底时使用的 Pydantic 模型，默认 IntentionResult（schemas/intent.py）。
        """
        self.router = router
        self.structured = structured
        self.llm_fallback_schema = llm_fallback_schema

    async def run(self, query: str, prev_intent: IntentType | None = None,
                  prev_user_input: str = "") -> IntentOutput:
        """对一次用户输入做完整意图识别，返回结构化意图结果。

        五级级联判定流程：
            1. 实体提取（正则）——从当前 query 提取所有实体。
            2. 选项答复检测（正则）——纯编号菜单选择直接产出 MENU_SELECTION，
               不经路由/LLM 重分类（选择动作的语义由发菜单的一方承载）。
            3. 追问检测（词表规则）——若为追问，继承上一轮意图，实体合并（本轮覆盖上轮）。
            4. 混合路由（BM25+稠密）——若命中非 general，直接产出。
            5. LLM 兜底（结构化输出）——注入近失候选，让 LLM 确认或改判。

        Args:
            query: 用户当前输入的原始文本。
            prev_intent: 上一轮识别出的意图类型（用于追问检测），首轮为 None。
            prev_user_input: 上一轮用户的原始输入（用于追问分支重跑上轮实体提取）。
                上轮实体不存储在会话中，而是用确定性正则重提取，保持零状态。
                调用方必须按三个参数调用，否则会触发 TypeError。

        Returns:
            IntentOutput 对象，包含最终意图类型、置信度、实体、来源步骤、重写查询等。
        """

        # ====== 第1级：实体提取（确定性正则） ======
        # 从当前输入中提取所有可能实体，不依赖任何模型
        entities = self._extract_entities(query)

        # ====== 第2级：选项答复检测（确定性正则，在追问之前） ======
        # 纯编号菜单选择是「选择」动作而非自由文本，不经 NLU 重分类——
        # 否则 score_threshold=0.0 的路由会以微小分数误命中任意意图（如实测
        # 「1」被路由到 set_research_topic），spawn 门禁随之误拦真实意图。
        # 命中即短路：MENU_SELECTION 可派发，派发权在 supervisor 对照其菜单。
        if is_option_reply(query):
            return IntentOutput(
                intent_type=IntentType.MENU_SELECTION, confidence=1.0,
                entities=entities, source=IntentStep.OPTION,
                prev_intent=prev_intent,
                rewritten_query=query,  # 选择动作不改写原文
            )

        # ====== 第3级：追问检测（词表启发式） ======
        # 若判定为追问，则继承上一轮意图，同时合并实体：上轮实体 + 本轮实体（同键覆盖）
        if self._detect_followup(query, prev_intent):
            # 若存在上轮输入，则重跑实体提取（获取上轮实体）
            prev_entities = extract_entities(prev_user_input) if prev_user_input else {}
            # 实体合并顺序：上轮在前，本轮在后，确保本轮同键实体覆盖上轮
            # 设计意图：追问场景下，用户可能仍依赖上轮的实体（如 PDF 路径），
            # 但也会补充或修正新的实体（如“Figure 3”）。合并后本轮新实体优先，
            # 避免丢失上轮有效信息，同时体现当前输入的最新指向。
            return IntentOutput(
                intent_type=prev_intent, confidence=1.0, # 追问直接继承，置信度置为 1
                entities={**prev_entities, **entities}, # Python 字典解包合并，后者（entities）的键值会覆盖前者（prev_entities）中同名的键。
                source=IntentStep.FOLLOWUP, prev_intent=prev_intent,
                rewritten_query=query # 追问不改写原文
            )

        # ====== 第4级：混合路由 ======
        # 调用混合路由器进行判定，若命中且结果不是 "general"，则直接产出
        choice = self.router(query)
        if choice is not None and choice.name != "general":
            # 将融合分数截断到 [0,1] 区间（余弦相似度可为负，稀疏点积可 >1，需归一化）
            return IntentOutput(
                intent_type=IntentType(choice.name),
                confidence=float(max(0.0, min(1.0, choice.similarity_score or 0.0))), # 融合分数 clip 到 [0,1]（cosine 可为负、稀疏点积可 >1，非概率）
                entities=entities, source=IntentStep.ROUTER, prev_intent=prev_intent,
                rewritten_query=query)

        # ====== 第5级：LLM 兜底 ======
        # 获取路由层近失候选（top-k 融合分数），供 LLM 参考，避免盲猜
        near_miss = self.router.scores(query, k=3)

        # 构建注入路由先验的提示词，调用结构化输出模块
        result = await self.structured.extract(
            prompt=self._build_llm_prompt(query, near_miss),
            schema=self.llm_fallback_schema,
            fallback=lambda: IntentionResult(intent_type=IntentType.GENERAL,
                                             confidence=0.0),
        )
        # steps/clarification 透传：复合意图拆分或澄清问题，由上层据此处理
        # 组装最终输出：透传 LLM 返回的 steps/clarification 供上层处理
        return IntentOutput(
            intent_type=result.intent_type,
            confidence=result.confidence,
            entities=entities, source=IntentStep.LLM, prev_intent=prev_intent,
            rewritten_query=result.query_rewrite or query, # 若 LLM 提供了改写则用，否则保留原文
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
        """构建 LLM 兜底提示词。

        注入四部分信息：
            1. 意图枚举列表（IntentType 所有取值）。
            2. 输出字段约定，含 clarification 的填写条件——模型没被交代条件就不会
               产出澄清，跨轮澄清链路（CLI 挂起 → 合并重跑 → 超轮强制调度）随之
               成为永不触发的死路径。
            3. 路由层的近失候选（路由名 + 分数），供 LLM 参考确认或改判。
            4. 原始用户输入 query。

        Args:
            query: 用户原始输入文本。
            near_miss: 路由层返回的 top-k 候选列表，每项为 (路由名, 分数)。

        Returns:
            组合后的提示词字符串。
        """
        parts = [
            "你是意图分类器。从以下意图中选择一个：",
            ", ".join(t.value for t in IntentType),
            "输出 JSON：{intent_type, confidence, query_rewrite, clarification}。",
            "clarification 可选，留空串表示不需要：只在输入缺决定性信息、无法在意图间取舍时才填，"
            "例如指代不明（「帮我处理一下那篇」没说哪篇）或动作不明（没说读、写笔记还是分析）。"
            "能推断出合理意图就不要澄清——直接给 intent_type，用 confidence 表达"
            "不确定程度；闲聊、求助、超出范围这类意图永远不需要澄清。",
            "澄清文本会原样展示给用户，须自足、简短、只问一个问题；即使填了澄清，"
            "也要照常给出最可能的 intent_type 与 confidence。",
        ]
        if near_miss:
            parts.append("路由层近失候选（供参考，可确认或改判）：")
            for name, score in near_miss:
                parts.append(f"  - {name}: {score:.3f}")
        parts.append(f"用户输入：{query}")
        return "\n".join(parts)
