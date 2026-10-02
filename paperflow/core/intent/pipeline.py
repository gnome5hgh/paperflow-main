# paperflow/core/intent/pipeline.py
"""意图识别五级级联编排。

五级级联（自顶向下逐级判定，前级未定夺才落到后级）：
- 实体提取：正则提取 PDF 路径/arXiv ID/DOI/Figure 等实体
- 选项答复检测：纯编号菜单选择直接产出 MENU_SELECTION（确定性正则，不重分类）
- 追问检测：判断是否承接上一轮意图（依赖会话中的上一轮意图）
- 混合路由：一次打分三用（spec 2026-10-02 §2）——multi-label 多命中产 steps /
  S1-S2 澄清判据 / LLM 兜底近失候选，均为对 scores() 输出的确定性过滤
- LLM 兜底：用结构化输出解析意图，注入路由近失候选供参考，改写缺省原文；
  clarification 的「问不问」由代码判据决定（§3），LLM 只负责问题文案

多意图双通道（spec 2026-10-02）：
- steps：路由 multi-label 命中 ≥2 业务意图，或 LLM 兜底拆分（触发契约不变）
- clarification：S1 贴线（top1 刚过自己的 accept 线）∨ S2 竞争（业务候选分差小）
  时强制澄清轮；非强制轮代码丢弃 LLM 产出的 clarification——触发权收归代码
"""
from pydantic import BaseModel

from paperflow.core.intent.schemas.intent import (
    INTENT_LABELS_ZH, INTENT_META,
    IntentOutput, IntentType, IntentStep, IntentionResult,
)
from paperflow.core.intent.routing.entities import extract_entities
from paperflow.core.intent.routing.followup import detect_followup
from paperflow.core.intent.routing.option_reply import is_option_reply

#: 复合拆分步数上限（与 IntentionResult._steps_guard 同值，源此处引用）。
MAX_STEPS = 3

#: 路由 multi-label 的后续命中独立自信线：score ≥ τ + EPSILON 才可入 steps
#: （spec 2026-10-02 §2.2-a）。分数为未归一化融合值（clip 后 [0,1]，非概率），
#: 阈值为 fit 逐路由标定的 accept 线；0.05 为保守初值——刚好过线的「搭车命中」
#: 不拆。标定方法：复合句评测集上扫 EPSILON，取 steps 精确率 × 召回率最大点。
#: 前置：阈值未标定（None 或 ≤0）的路由不参与拆分（见 run() 第 4 级注释）。
ROUTER_STEPS_EPSILON = 0.05

#: S1 贴线判据容差：top1 分数 < τ + FLOOR_DELTA 即视为不够自信（含未过线情形）
#: （spec 2026-10-02 §3.1）。同分数量纲，标定方法同上。
CLARIFY_FLOOR_DELTA = 0.05

#: S2 竞争判据：业务候选 top1 − top2 < MARGIN 即视为无法取舍（spec 2026-10-02 §3.1）。
CLARIFY_MARGIN = 0.05


def _is_business(name: str) -> bool:
    """路由名是否为可派发业务意图；未知路由名（不在枚举）一律非业务。"""
    try:
        return INTENT_META[IntentType(name)][1]
    except KeyError:
        return False


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
            4. 混合路由（BM25+稠密）——multi-label 裁决：单业务命中走澄清判据；
               ≥2 业务命中产 steps 短路；非业务命中直接产出。
            5. LLM 兜底（结构化输出）——强制澄清轮或常规解析（clarification 仅在
               代码判定强制时保留）。

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

        # ====== 第4级：混合路由（一次打分三用） ======
        # scores() 与 __call__ 同数据源同聚合（按路由分组均值、降序），一次编码一次
        # 检索同时供给：multi-label 过滤、S1/S2 澄清判据、LLM 兜底近失候选——
        # 替代原先 __call__ + scores() 的两次编码。__call__ 语义不变（fit/eval 用）。
        scored = self.router.scores(query, k=self.router.top_k)

        # 多标签过滤：过各自生效阈值（路由专属优先，全局 None = 无门槛恒过，同 __call__）
        passed = [(name, score) for name, score in scored
                  if self._passes(name, score)]

        if passed:
            top_name, top_score = passed[0]
            if _is_business(top_name):
                # --- multi-label：后续命中须过「自身阈值 + EPSILON」且为业务意图 ---
                # 阈值未标定（None 或 ≤0，routes.yaml 出厂态）的路由不参与拆分——
                # 没有标定 accept 线就没有「独立自信」可言，τ=0 时 EPSILON 形同
                # 虚设（实测 FakeEmbedder 下任意第二高分 0.86+ 都会被误拆）。
                # 多标签是标定后的能力：fit 写回阈值之前，管线退化为单标签（同旧）。
                steps_names = [top_name]
                for name, score in passed[1:]:
                    if len(steps_names) >= MAX_STEPS:
                        break
                    threshold = self._effective_threshold(name)
                    if threshold is None or threshold <= 0.0:
                        continue
                    if (score >= threshold + ROUTER_STEPS_EPSILON
                            and _is_business(name)):
                        steps_names.append(name)
                if len(steps_names) >= 2:
                    # 复合句被路由直接拆分：短路返回不进 LLM 兜底。
                    # intent_type = steps[0]（首步即主意图），spawn 门禁按 steps 队列放行。
                    return IntentOutput(
                        intent_type=IntentType(steps_names[0]),
                        confidence=self._clip01(top_score),
                        entities=entities, source=IntentStep.ROUTER,
                        prev_intent=prev_intent, rewritten_query=query,
                        steps=[IntentType(n) for n in steps_names])
                # --- 单业务命中：S1/S2 澄清判据（spec 2026-10-02 §3） ---
                if self._ambiguous(scored):
                    return await self._clarify_round(
                        query, entities, prev_intent, scored)
                # 自信单意图：现状直出，无澄清无拆分
                return IntentOutput(
                    intent_type=IntentType(top_name),
                    confidence=self._clip01(top_score),
                    entities=entities, source=IntentStep.ROUTER,
                    prev_intent=prev_intent, rewritten_query=query)
            # 非业务命中（闲聊/超范围等）：现状直出，永不澄清/拆分
            return IntentOutput(
                intent_type=IntentType(top_name),
                confidence=self._clip01(top_score),
                entities=entities, source=IntentStep.ROUTER,
                prev_intent=prev_intent, rewritten_query=query)

        # ====== 路由全未命中：先过澄清判据，再落 LLM 兜底 ======
        if self._ambiguous(scored):
            return await self._clarify_round(query, entities, prev_intent, scored)

        # ====== 第5级：LLM 兜底（常规解析） ======
        # clarification 触发权在代码：非强制轮 LLM 即使产出也丢弃（steps 非空时
        # schema 互斥护栏已清，此处对「有 clarification 但没拆」的违命输出收口）
        result = await self._llm_extract(query, scored, force_clarification=False)
        return IntentOutput(
            intent_type=result.intent_type,
            confidence=result.confidence,
            entities=entities, source=IntentStep.LLM, prev_intent=prev_intent,
            rewritten_query=result.query_rewrite or query, # 若 LLM 提供了改写则用，否则保留原文
            steps=result.steps or [],
            clarification=None,
        )

    # ------------------------------------------------------------------
    # 级联前级委托（保留方法形态便于测试替换）
    # ------------------------------------------------------------------

    def _extract_entities(self, query: str) -> dict:
        """实体提取（委托 entities.extract_entities；保留方法形态便于测试替换）。"""
        return extract_entities(query)

    def _detect_followup(self, query: str, prev_intent) -> bool:
        """追问检测（委托 followup.detect_followup；保留方法形态便于测试替换）。"""
        return detect_followup(query, prev_intent)

    # ------------------------------------------------------------------
    # 路由判定辅助
    # ------------------------------------------------------------------

    def _effective_threshold(self, name: str) -> float | None:
        """路由生效阈值：路由专属优先，否则全局；全局也未设则 None（无门槛恒过）。

        与 router._pass_routes 的阈值选取逻辑保持一致（router.py:236-238）。
        """
        route = self.router.get(name)
        if route is not None and route.score_threshold is not None:
            return route.score_threshold
        return self.router.score_threshold

    def _passes(self, name: str, score: float) -> bool:
        """单路由阈值裁决：阈值未设恒过，否则 score >= 阈值。"""
        threshold = self._effective_threshold(name)
        return True if threshold is None else score >= threshold

    def _clip01(self, score: float) -> float:
        """融合分数截断到 [0,1]（cosine 可为负、稀疏点积可 >1，非概率）。"""
        return max(0.0, min(1.0, score))

    def _ambiguous(self, scored: list[tuple[str, float]]) -> bool:
        """S1/S2 澄清判据（spec 2026-10-02 §3.1）——只看业务意图候选。

        S1 贴线：业务 top1 分数 < 自身生效阈值 + FLOOR_DELTA（含未过线情形——
        「刚过线」与「差一点」都是不自信）。
        S2 竞争：业务候选 top1 − top2 < MARGIN（两个业务意图分数贴着，无法取舍）。
        非业务候选（闲聊/超范围）不参与——那些永远不需要澄清。
        """
        biz = [(name, score) for name, score in scored
               if score > 0 and _is_business(name)]
        if not biz:
            return False
        top_name, top_score = biz[0]
        # S1：阈值未标定（None 或 ≤0）时无从「贴线」，不触发——标定前澄清不启用，
        # 路由表现与旧行为一致（ miss 才落 LLM 兜底）
        threshold = self._effective_threshold(top_name)
        s1 = (threshold is not None and threshold > 0.0
              and top_score < threshold + CLARIFY_FLOOR_DELTA)
        # S2：存在第二个业务候选且分差小于 MARGIN
        s2 = (len(biz) >= 2
              and (top_score - biz[1][1]) < CLARIFY_MARGIN)
        return bool(s1 or s2)

    # ------------------------------------------------------------------
    # LLM 兜底
    # ------------------------------------------------------------------

    async def _clarify_round(self, query: str, entities: dict,
                             prev_intent: IntentType | None,
                             scored: list[tuple[str, float]]) -> IntentOutput:
        """强制澄清轮：进 LLM 兜底产出澄清问题，文案权在模型、触发权在代码。

        LLM 违命未产出 clarification 时，用业务候选 top2 合成模板兜底问题——
        判据说了「要问」就一定要问出去，否则澄清链路退化为死路径（上次修复教训）。
        intent_type/confidence 照常产出：澄清是附加通道，不改变单标签答案，
        评估指标不受影响。
        """
        result = await self._llm_extract(query, scored, force_clarification=True)
        clarification = result.clarification or self._synthesize_clarification(scored)
        return IntentOutput(
            intent_type=result.intent_type,
            confidence=result.confidence,
            entities=entities, source=IntentStep.LLM, prev_intent=prev_intent,
            rewritten_query=result.query_rewrite or query,
            steps=[],  # 澄清轮不拆分（schema 互斥护栏兜底，此处显式为空）
            clarification=clarification,
        )

    async def _llm_extract(self, query: str, scored: list[tuple[str, float]],
                           force_clarification: bool) -> IntentionResult:
        """调结构化输出模块做 LLM 兜底，注入路由近失候选（top-k 融合分数）。"""
        return await self.structured.extract(
            prompt=self._build_llm_prompt(query, scored,
                                          force_clarification=force_clarification),
            schema=self.llm_fallback_schema,
            fallback=lambda: IntentionResult(intent_type=IntentType.UNCLASSIFIED,
                                             confidence=0.0),
        )

    def _synthesize_clarification(self, scored: list[tuple[str, float]]) -> str:
        """合成兜底澄清问题：业务候选 top2 用二选一模板，仅一个用开放确认模板。"""
        biz = [IntentType(name) for name, score in scored
               if score > 0 and _is_business(name)][:2]
        labels = [INTENT_LABELS_ZH.get(t, t.value) for t in biz]
        if len(labels) >= 2:
            return f"你想让我「{labels[0]}」还是「{labels[1]}」？"
        if labels:
            return f"你是想让我「{labels[0]}」吗？可以再说得具体一点。"
        return "你的需求是什么？可以再说得具体一点。"

    def _build_llm_prompt(self, query: str, near_miss: list[tuple[str, float]],
                          force_clarification: bool = False) -> str:
        """构建 LLM 兜底提示词。

        注入四部分信息：
            1. 意图枚举列表（IntentType 所有取值）。
            2. 输出字段约定：steps 填写条件；clarification 条款按 force_clarification
               切换——强制轮必须产出（触发权在代码），常规轮仅在缺决定性信息时填
               （管线会丢弃，此处条款保留是给模型一致的输出契约）。
            3. 路由层的近失候选（路由名 + 分数），供 LLM 参考确认或改判。
            4. 原始用户输入 query。

        Args:
            query: 用户原始输入文本。
            near_miss: 路由层 top-k 候选列表，每项为 (路由名, 分数)。
            force_clarification: 强制澄清轮标记（spec 2026-10-02 §3.2）。

        Returns:
            组合后的提示词字符串。
        """
        parts = [
            "你是意图分类器。从以下意图中选择一个：",
            ", ".join(t.value for t in IntentType),
            "输出 JSON：{intent_type, confidence, query_rewrite, steps, clarification}。",
        ]
        if force_clarification:
            parts.append(
                "本轮必须产出 clarification（写给用户的一句简短澄清问题）："
                "输入在多个意图间存在歧义，需要用户补充信息后才能执行。"
                "澄清文本会原样展示给用户，须自足、简短、只问一个问题。"
                "steps 必须留空（澄清轮不拆分）。"
            )
        else:
            parts.extend([
                "steps 仅当输入包含 ≥2 个相互独立、分属不同意图的业务动作时才填：每个 "
                "step 是一个业务意图名（可派发类），按执行顺序排列，最多 3 步，且 "
                "steps[0] 必须等于 intent_type；单一动作或拿不准时必须留空（宁可不拆）。"
                "拆分时 intent_type 取第一步。",
                "clarification 可选，留空串表示不需要：只在输入缺决定性信息、无法在意图间取舍时才填，"
                "例如指代不明（「帮我处理一下那篇」没说哪篇）或动作不明（没说读、写笔记还是分析）。"
                "能推断出合理意图就不要澄清——直接给 intent_type，用 confidence 表达"
                "不确定程度；闲聊、求助、超出范围这类意图永远不需要澄清。",
                "澄清文本会原样展示给用户，须自足、简短、只问一个问题；即使填了澄清，"
                "也要照常给出最可能的 intent_type 与 confidence。",
            ])
        if near_miss:
            parts.append("路由层近失候选（供参考，可确认或改判）：")
            for name, score in near_miss:
                parts.append(f"  - {name}: {score:.3f}")
        parts.append(f"用户输入：{query}")
        return "\n".join(parts)
