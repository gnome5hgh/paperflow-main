# paperflow/core/intent/pipeline.py
"""意图识别五级级联编排。

五级级联（自顶向下逐级判定，前级未定夺才落到后级）：
- 实体提取：正则提取 PDF 路径/arXiv ID/DOI/Figure 等实体
- 选项答复检测：纯编号菜单选择直接产出 MENU_SELECTION（确定性正则，不重分类）
- 追问检测：判断是否承接上一轮意图（依赖会话中的上一轮意图）
- 混合路由：对 scores() 的输出做一组确定性过滤，一次打分派三个用场——
  ① 多标签裁决：不止一个业务意图过了各自的标定阈值时，直接拆成有序 steps；
  ② 澄清判据：分数贴着阈值线、或两个业务候选分数咬得很近时，转入强制澄清轮；
  ③ 给 LLM 兜底提供近失候选（没过线但分数靠前的意图，供模型参考改判）
- LLM 兜底：用结构化输出解析意图，注入路由近失候选供参考，改写缺省原文

一句话多意图的处理走两条通道，共同原则是「要不要」由代码判据决定，「怎么做」
才交给 LLM：
- steps（拆分执行）：路由层多命中直接拆，或 LLM 兜底拆（提示词约定何时拆）。
  顺序性由 spawn 门禁的 steps 队列在代码层强制（见 runtime._pending_steps 与
  spawn._admit），不依赖 supervisor 提示词自觉。
- clarification（澄清反问）：触发与否完全由上面 ② 的分数判据决定——分数够自信
  时，LLM 即使在输出里写了澄清也会被丢弃；判据说要问时，LLM 必须写出问题，
  写不出来就用模板合成。这样「问不问」永远确定，「问什么」才交给模型。
"""
from pydantic import BaseModel

from paperflow.core.intent.schemas.intent import (
    INTENT_LABELS_ZH, INTENT_META, MAX_STEPS,
    IntentOutput, IntentType, IntentStep, IntentionResult,
)
from paperflow.core.intent.routing.entities import extract_entities
from paperflow.core.intent.routing.confirm import format_intent_options
from paperflow.core.intent.routing.followup import detect_followup
from paperflow.core.intent.routing.option_reply import is_option_reply

# ── 路由 / 澄清判据（本文件消费；标定脚本 apply_calibration.py 就地改写） ────

#: 多标签拆分的「独立自信」余量。
#: - 值：0.02。
#: - 含义与单位：候选意图的融合分数须超过「自身标定阈值 + 本余量」才有资格拆进
#:   steps；仅仅压着阈值线过线不算数。分数为稠密/稀疏两路融合的未归一化值
#:   （截断后落 [0,1]，不是概率），本值与之同量纲。
#: - 改它的后果：必须重跑 steps/澄清标定评测（在复合句评测集上扫该值，取 steps
#:   精确率×召回率最高点）；直接改变多标签拆分口径与评测指标。
ROUTER_STEPS_EPSILON = 0.02

#: 「贴线」澄清判据的容差。
#: - 值：0.05。
#: - 含义与单位：业务候选里分数最高者，若分数低于「自身标定阈值 + 本容差」视为
#:   不够自信（刚好压线通过或差一点没过都算），转入强制澄清轮。与 ROUTER_STEPS_EPSILON
#:   同量纲，无量纲比值。
#: - 改它的后果：改变澄清触发口径，需在复合句/歧义句评测集上重标定；影响
#:   澄清率与路由指标评测口径。
CLARIFY_FLOOR_DELTA = 0.05

#: 「竞争」澄清判据的分差线。
#: - 值：0.05。
#: - 含义与单位：分数最高的两个业务候选意图分差小于此值时，认为两个都有可能、
#:   路由器无法取舍（如「这本书讲什么」落在问答与精读分析之间），让用户二选一。
#:   与上述两值同量纲。
#: - 改它的后果：改变澄清触发口径，需重标定；影响澄清率与评测指标。
CLARIFY_MARGIN = 0.05


def _is_business(name: str) -> bool:
    """路由名是否为可派发业务意图；未知路由名（不在枚举）一律非业务。"""
    try:
        return INTENT_META[IntentType(name)][1]
    except KeyError:
        return False


#: 路由前要剥离的实体键——路径类实体。文件名常含论文主题词（如
#: ".../Link-Prediction-in-Knowledge-Graphs.pdf"），整段进入编码器后，稀疏与稠密
#: 两路都会被主题词拽向「主题相近」的意图（实测「<知识图谱链接预测的 PDF 路径>
#: 这篇论文是干什么的」被 set_research_topic 以 0.716 险胜 analyze_paper 0.687——
#: 动作词「这篇论文是干什么的」单独路由时两路分数都到不了该意图）。意图由用户
#: 的动作词决定，路径只是载荷：剥离后再路由，剥离后为空白则回退原文（纯路径
#: 输入仍可路由）。arxiv_id/doi/figure 短且不含主题词序列，不剥离。
_ROUTING_STRIP_KEYS = ("pdf_path", "note_path")


def routing_text(query: str, entities: dict) -> str:
    """剥离路径类实体后的路由文本：意图信号只来自用户的动作词，不来自文件名。

    模块级函数而非方法：alpha sweep / steps 评测的矩阵路径必须复刻同一剥离，
    否则评测口径与生产不一致（管线第 4 级与两处评测共用本函数）。

    Args:
        query: 用户原始输入。
        entities: 第 1 级 extract_entities 的产出（本函数只读）。

    Returns:
        剥离路径后的文本（空白收敛为单空格）；剥完为空白则原样返回 query。
    """
    text = query
    for key in _ROUTING_STRIP_KEYS:
        value = entities.get(key)
        if value:
            text = text.replace(value, " ")
    text = " ".join(text.split())
    return text if text else query


def is_ambiguous(scored: list[tuple[str, float]],
                 threshold_of) -> bool:
    """模块级澄清判据：路由分数层面的两条「不自信」信号（S1 贴线 / S2 竞争）。

    模块级纯函数而非方法：steps/澄清评测（steps_eval.py）的矩阵路径必须复刻
    同一份判据，评测口径才不会与生产漂移；IntentPipeline._ambiguous 委托本函数。

    只看业务意图候选（可派发的那几类），闲聊/超范围这类永远不参与：它们要么
    轻回复要么拒绝，不存在选错方向执行下去的代价。两条信号满足任一即澄清：

    - S1 贴线：分数最高的业务候选，分数低于（自身标定阈值 + CLARIFY_FLOOR_DELTA）。
      「刚压线通过」和「差一点没过」在这里是同一回事——路由器都没能把它和
      其他意图拉开差距，硬选一个大概率选错。
    - S2 竞争：分数最高的两个业务候选分差小于 CLARIFY_MARGIN。两个意图都有可能
      （典型如「这本书讲什么」落在问答和精读分析之间），让用户二选一比赌
      一个便宜得多。

    两条判据都锚定在 fit 标定的阈值上，所以有个共同前提：top1 候选的路由
    阈值必须是标定过的（> 0）。routes.yaml 出厂态阈值全 0.0，那时路由层本来
    就没有「认准」的能力可言，判据整体不启用，行为与旧版一致（未命中才落
    LLM 兜底）。

    Args:
        scored: [(路由名, 融合分数)]，按分数降序（router.scores 的输出形态）。
        threshold_of: 路由名 → 生效阈值（路由专属优先，否则全局；未设为 None）。

    Returns:
        True 表示该向用户澄清（触发强制澄清轮）。
    """
    biz = [(name, score) for name, score in scored
           if score > 0 and _is_business(name)]
    if not biz:
        return False
    top_name, top_score = biz[0]
    # S1 贴线：阈值未标定时不启用（见 docstring 末段）
    threshold = threshold_of(top_name)
    barely_confident = (threshold is not None and threshold > 0.0
                        and top_score < threshold + CLARIFY_FLOOR_DELTA)
    # S2 竞争：第二名也是业务意图，且和第一名咬得很近
    runner_up_close = (len(biz) >= 2
                       and (top_score - biz[1][1]) < CLARIFY_MARGIN)
    return bool(barely_confident or runner_up_close)


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
        entities = extract_entities(query)

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
        if detect_followup(query, prev_intent):
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
        # 这里只调 scores()，不再调 router(query)——两者的打分来自同一份索引、
        # 同一种「按路由分组取均值再降序」的聚合，但 __call__ 只回一个 argmax 命中，
        # scores() 把每个路由的分数全数给出，正好够下面三件事共用：
        # ① 按各自阈值过滤出所有「过线」路由（多标签拆分的原料）；
        # ② 喂给 _ambiguous() 算「要不要澄清」（它需要看没过线的候选分数）；
        # ③ 喂给 LLM 兜底当近失候选（原先在第 5 级还要再调一次 scores()，省了）。
        # __call__ 本身一个字没改，fit/eval/sweep 走的还是老路径，单标签指标不受影响。
        # 路由输入用剥离路径实体后的文本（routing_text）——评分/拆分/澄清三件事
        # 共用这份 stripped 口径；LLM 兜底仍看原文（模型对路径鲁棒，且改写契约基于原文）。
        scored = self.router.scores(routing_text(query, entities),
                                    k=self.router.top_k)

        # 多标签过滤：过各自生效阈值（路由专属优先，全局 None = 无门槛恒过，同 __call__）
        passed = [(name, score) for name, score in scored
                  if self._passes(name, score)]

        if passed:
            top_name, top_score = passed[0]
            if _is_business(top_name):
                # --- 多意图标签拆分，后续候选意图要同时满足三个条件才能拆进 steps：
                # ① 它自己这条路由的阈值是 fit 标定过的（> 0）。routes.yaml 出厂时
                #    阈值全是 0.0，此时「过线」毫无含金量——任何第二高分都能过一条
                #    0.0 的线，整句会被拆得面目全非（实测未标定状态下任意复合句的
                #    第二高分都在 0.86 以上）。所以标定之前路由层不拆分，行为与
                #    旧版单标签完全一致；fit 写回真实阈值后拆分才自然激活。
                # ② 分数超过「自身阈值 + ROUTER_STEPS_EPSILON」——与主意图同一条
                #    规则，独立裁决在重扫分数上（业界多标签惯例：全类打分 + 逐类
                #    阈值，无第二名的特殊放宽）。
                # ③ 是可派发的业务意图：闲聊、帮助这类系统意图永远不该出现在steps 里——它们不派发，拆进去只会让 spawn 门禁拒掉整条链。
                #
                # 重扫窗口 = 路由数量（get_thresholds 的键数，随新增意图自动增长，
                # 不设常量——2026-10-06 评审定）：主管道 top_k=3 截断会让 88 条
                # 复合句里 72 条的第二意图拿哨兵分（不进候选），完整阈值门漏拆
                # 82%（scripts/intent/eval 2026-10-06）。注意 scores(k) 的 k 数的
                # 是例句条数：以路由数为窗口保证每个路由至少一个例句的曝光位。
                # 若要覆盖「全部例句」（字面全类打分），会改变路由分聚合口径，
                # 须先重标阈值再切（实测直接切换误拆翻倍，见 eval README）。
                steps_names = [top_name]
                rescored = self.router.scores(
                    routing_text(query, entities),
                    k=len(self.router.get_thresholds()))
                for name, score in rescored:
                    if name == top_name or not _is_business(name):
                        continue
                    if len(steps_names) >= MAX_STEPS:
                        break
                    threshold = self._effective_threshold(name)
                    if threshold is None or threshold <= 0.0:
                        continue
                    if score >= threshold + ROUTER_STEPS_EPSILON:
                        steps_names.append(name)
                if len(steps_names) >= 2:
                    # 至少两个业务意图都「认准了」→ 这是一句复合请求，直接在路由层
                    # 拆开短路返回，不进 LLM 兜底。主意图取第一步（intent_type =
                    # steps[0]），spawn 门禁会按这个 steps 列表建队列，逐个校验
                    # supervisor 的派发顺序。
                    return IntentOutput(
                        intent_type=IntentType(steps_names[0]),
                        confidence=self._clip01(top_score),
                        entities=entities, source=IntentStep.ROUTER,
                        prev_intent=prev_intent, rewritten_query=query,
                        steps=[IntentType(n) for n in steps_names])
                # --- 只有一个业务意图过线：先问一句「要不要向用户澄清」 ---
                # 路由认准了但认得吃力（分数贴线）或有人和它咬得很近（分差小）时，与其硬选一个意图执行错方向，不如让用户补一句话。
                if self._ambiguous(scored):
                    return await self._clarify_round(
                        query, entities, prev_intent, scored)
                # 路由认得又准又稳：直接产出单意图，不澄清不拆分——这是绝大多数输入的快路径，一次 LLM 调用都不花
                return IntentOutput(
                    intent_type=IntentType(top_name),
                    confidence=self._clip01(top_score),
                    entities=entities, source=IntentStep.ROUTER,
                    prev_intent=prev_intent, rewritten_query=query)
            # 分数最高的是闲聊/超范围这类系统意图：直接照旧产出，不澄清也不拆——
            # 这类输入要么轻回复要么明确拒绝，不存在「在意图间取舍」的问题
            return IntentOutput(
                intent_type=IntentType(top_name),
                confidence=self._clip01(top_score),
                entities=entities, source=IntentStep.ROUTER,
                prev_intent=prev_intent, rewritten_query=query)

        # ====== 路由全未命中：同样先过一遍澄清判据 ======
        # 未命中不代表没有候选——scored 里还有没过线的「近失」意图。若近失候选里
        # 有业务意图且分数贴线/互相咬近，说明用户输入处在几个意图的模糊地带，
        # 值得问一句；否则才真正交给 LLM 兜底盲解析。
        if self._ambiguous(scored):
            return await self._clarify_round(query, entities, prev_intent, scored)

        # ====== 第5级：LLM 兜底（常规解析） ======
        # 走到这里说明代码判据认为「不需要澄清」。但 LLM 拿到输入后仍可能自作主张产出 clarification——
        # 一律丢弃：澄清的「问不问」只认代码判据，否则等于又把触发权交回给模型心证。
        # steps 照常透传：LLM 拆分是复合意图在第四级时没有成功拆分后的第二个兜底，触发契约在提示词里约定；
        # steps 非空时 schema 护栏会自动清掉 clarification，这里再显式置 None，把「拆了还要问」的违命输出也收口。
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
        """判断当前输入是否值得向用户澄清——委托模块级 is_ambiguous（S1/S2 判据
        与判定表见其 docstring；判定表单测在 tests/intent/test_pipeline.py）。"""
        return is_ambiguous(scored, self._effective_threshold)

    # ------------------------------------------------------------------
    # LLM 兜底
    # ------------------------------------------------------------------

    async def _clarify_round(self, query: str, entities: dict,
                             prev_intent: IntentType | None,
                             scored: list[tuple[str, float]]) -> IntentOutput:
        """强制澄清轮：代码判据认定「该问了」，让 LLM 把问题写出来。

        分工是「触发权在代码、文案权在模型」：要不要问由 _ambiguous() 的分数
        判据说了算，这里只负责让 LLM 生成一句能展示给用户的澄清问题。两个兜底
        设计保证「说要问就一定问出去」：

        - LLM 违命没写 clarification 时，用业务候选前两名合成模板问题（二选一
          问法；只有一个候选就开放式确认）。若连模板都不兜底，判据白算、澄清
          链路退化成永远不触发的死路径——这正是上一版澄清机制修过的病。
        - intent_type/confidence 照常产出不缺席：澄清是搭在正常识别结果上的
          附加通道，不改变单标签答案，所以离线评估指标不受澄清轮影响。

        steps 显式为空：澄清和拆分互斥——都要拆了就不需要问，都要问了就别拆。

        候选回传锚点（2026-10-04 澄清统一）：业务候选 top2 写入
        clarify_candidates，问题末尾由代码追加编号选项行（format_intent_options）
        ——文案权在模型、选项枚举权在代码：散文式提问无法保证可解析，编号行
        保证用户回复经 match_option_choice 确定性解析回意图，落地为
        会话意图（runtime 在 run 内同步问、代码级落地，无跨轮状态）。
        """
        candidates = [IntentType(name) for name, score in scored
                      if score > 0 and _is_business(name)][:2]
        result = await self._llm_extract(query, scored, force_clarification=True)
        clarification = result.clarification or self._synthesize_clarification(scored)
        if candidates:
            clarification = f"{clarification}\n{format_intent_options(candidates)}"
        return IntentOutput(
            intent_type=result.intent_type,
            confidence=result.confidence,
            entities=entities, source=IntentStep.LLM, prev_intent=prev_intent,
            rewritten_query=result.query_rewrite or query,
            steps=[],  # 澄清轮不拆分
            clarification=clarification,
            clarify_candidates=candidates,
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
        """合成兜底澄清问题（LLM 违命没写 clarification 时用）。

        按业务候选分数取前两名：有两个就二选一地问（「你想让我「A」还是「B」？」），
        只有一个就开放式确认。标签用 INTENT_LABELS_ZH 的中文短名——这个问题会
        原样打给用户，枚举英文值用户看不懂。"""
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
            near_miss: 路由层 top-k 候选列表，每项为 (路由名, 分数)——含未过阈值
                线的候选，供 LLM 在路由先验上确认或改判，而非盲猜。
            force_clarification: 是否强制澄清轮。True 时提示词改为「必须产出
                clarification」且禁用 steps——调用方（_clarify_round）已用分数
                判据认定该问，提示词只负责把问题文案要出来。

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
                f"step 是一个业务意图名（可派发类），按执行顺序排列，最多 {MAX_STEPS} 步，且 "
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
