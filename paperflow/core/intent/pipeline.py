# paperflow/core/intent/pipeline.py
"""意图识别五级级联编排。

五级级联（自顶向下逐级判定，前级未定夺才落到后级）：
- 实体提取：正则提取 PDF 路径/arXiv ID/DOI/Figure 等实体
- 选项答复检测：纯编号菜单选择直接产出 MENU_SELECTION（确定性正则，不重分类）
- 追问检测：判断是否承接上一轮意图（依赖会话中的上一轮意图）
- 混合路由：对 scores() 的输出做一组确定性过滤，一次打分派三个用场——
  ① 多标签裁决：不止一个业务意图过了各自的标定阈值时，直接拆成有序意图列表；
  ② 澄清判据：分数贴着阈值线、或两个业务候选分数咬得很近时，转入强制澄清轮；
  ③ 给 LLM 兜底提供近失候选（没过线但分数靠前的意图，供模型参考改判）
- LLM 兜底：用结构化输出解析意图，注入路由近失候选供参考，改写缺省原文

一句话多意图的处理走两条通道，共同原则是「要不要」由代码判据决定，「怎么做」
才交给 LLM：
- 意图列表（拆分执行）：路由层多命中直接拆，或 LLM 兜底拆（提示词约定何时拆）。
  拆分结果只是一条信号——识别出的意图会随 INTENT 块注入，并在收尾时作为事实
  摆给 supervisor 自查；实际派发顺序与并行由 supervisor 自主决定，框架不强制。
- clarification（澄清反问）：触发与否完全由上面 ② 的分数判据决定——分数够自信
  时，LLM 即使在输出里写了澄清也会被丢弃；判据说要问时，LLM 必须写出问题，
  写不出来就用模板合成。这样「问不问」永远确定，「问什么」才交给模型。
"""
from pydantic import BaseModel

from paperflow.core.intent.schemas.intent import (
    INTENT_LABELS_ZH, INTENT_META,
    ArbitrationChoice, IntentOutput, IntentType, IntentStep, IntentUnit,
    IntentionResult,
)
from paperflow.core.intent.routing.entities import extract_entities
from paperflow.core.intent.routing.confirm import format_intent_options
from paperflow.core.intent.routing.followup import detect_followup
from paperflow.core.intent.routing.option_reply import is_option_reply

# ── 路由 / 澄清判据常量 ──────────────────────────────────────────────────────

#: 多标签拆分的「独立自信」余量。
#: - 值：0.02。
#: - 含义与单位：候选意图的融合分数须超过「自身标定阈值 + 本余量」才有资格拆进
#:   steps；仅仅压着阈值线过线不算数。分数为稠密/稀疏两路融合的未归一化值
#:   （截断后落 [0,1]，不是概率），本值与之同量纲。
#: - 改它的后果：直接改变多标签拆分口径，调整取值前须在复合句题集上重新权衡
#:   拆分的精确率与召回率。
ROUTER_STEPS_EPSILON = 0.02

#: 「贴线」澄清判据的容差。
#: - 值：0.05。
#: - 含义与单位：业务候选里分数最高者，若分数低于「自身标定阈值 + 本容差」视为
#:   不够自信（刚好压线通过或差一点没过都算），转入强制澄清轮。与 ROUTER_STEPS_EPSILON
#:   同量纲，无量纲比值。
#: - 改它的后果：改变澄清触发口径——调大问得多（打扰用户）、调小问得少
#:   （该问不问），调整前须在歧义句题集上重新权衡。
CLARIFY_FLOOR_DELTA = 0.05

#: 「分差」判据的共用线：边界仲裁先问 LLM，仲裁不成再由「竞争」澄清问用户。
#: - 值：0.15。
#: - 含义与单位：单业务意图过线后，第二名业务候选与第一名的分差小于此值时，
#:   说明路由器没把两个候选拉开可信差距。处置分两级：先交给 LLM 结合意图定义
#:   二选一仲裁；仲裁失败（异常/越出候选）则转入澄清判据问用户。两边共用同一条
#:   线——若澄清用小线（如 0.05），分差在带外时仲裁失败会直接硬选第一名，把
#:   没把握的判定放行。与融合分数同量纲。
#: - 改它的后果：调大 → 更多请求进仲裁与澄清（边界纠错↑，LLM 成本与打扰↑）；
#:   调小 → 更少仲裁/澄清，更多贴近案例按路由器判定放行。
ROUTER_ARBITRATION_MARGIN = 0.15


def _is_business(name: str) -> bool:
    """路由名是否为可派发业务意图；未知路由名（不在枚举）一律非业务。

    Args:
        name: 路由名（路由层的字符串标签）。

    Returns:
        True 表示该意图可派发执行（非闲聊/帮助/超范围）。
    """
    try:
        return INTENT_META[IntentType(name)][1]
    except KeyError:
        return False


#: 路由前要剥离的实体键——路径类实体。文件名常含论文主题词（如
#: ".../Link-Prediction-in-Knowledge-Graphs.pdf"），整段进入编码器后，稀疏与稠密
#: 两路都会被主题词拽向「主题相近」的意图（如「<某论文路径> 这篇论文是干什么的」
#: 会险胜给主题相近的意图，而动作词「这篇论文是干什么的」单独路由时两路分数
#: 都到不了 analyze_paper 一类意图）。意图由用户
#: 的动作词决定，路径只是载荷：剥离后再路由，剥离后为空白则回退原文（纯路径
#: 输入仍可路由）。arxiv_id/doi/figure 短且不含主题词序列，不剥离。
_ROUTING_STRIP_KEYS = ("pdf_path", "note_path")


def routing_text(query: str, entities: dict) -> str:
    """剥离路径类实体后的路由文本：意图信号只来自用户的动作词，不来自文件名。

    模块级函数而非方法：离线打分路径要复刻同一剥离口径，两者共用本函数
    （管线第 4 级也用它），单独改任何一处都会让口径漂移。

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

    模块级纯函数而非方法：离线打分路径要复刻同一份判据，两者共用本函数
    才不会口径漂移；IntentPipeline._ambiguous 委托本函数。

    只看业务意图候选（可派发的那几类），闲聊/超范围这类永远不参与：它们要么
    轻回复要么拒绝，不存在选错方向执行下去的代价。两条信号满足任一即澄清：

    - S1 贴线：分数最高的业务候选，分数低于（自身标定阈值 + CLARIFY_FLOOR_DELTA）。
      「刚压线通过」和「差一点没过」在这里是同一回事——路由器都没能把它和
      其他意图拉开差距，硬选一个大概率选错。
    - S2 竞争：分数最高的两个业务候选分差小于 ROUTER_ARBITRATION_MARGIN（与边界
      仲裁共用同一条分差线，见常量注释）。两个意图都有可能（典型如「这本书讲
      什么」落在问答和精读分析之间），让用户二选一比赌一个便宜得多。

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
    # 未标定（阈值 None/≤0）→ 判据整体不启用（见 docstring 末段）：S1/S2 都锚定在
    # 标定阈值上——一条 0.0 的线让「过线候选」毫无含金量，「贴线」与「咬得近」
    # 随之失去意义，标定前必须退化为旧版单标签行为。
    threshold = threshold_of(top_name)
    if threshold is None or threshold <= 0.0:
        return False
    # S1 贴线：最高分业务候选压着自身阈值
    barely_confident = top_score < threshold + CLARIFY_FLOOR_DELTA
    # S2 竞争：第二名也是业务意图，且和第一名咬得很近
    runner_up_close = (len(biz) >= 2
                       and (top_score - biz[1][1]) < ROUTER_ARBITRATION_MARGIN)
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
                intents=[IntentUnit(intent_type=IntentType.MENU_SELECTION, confidence=1.0)],
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
                intents=[IntentUnit(intent_type=prev_intent, confidence=1.0)],  # 追问直接继承，置信度置为 1
                entities={**prev_entities, **entities}, # Python 字典解包合并，后者（entities）的键值会覆盖前者（prev_entities）中同名的键。
                source=IntentStep.FOLLOWUP, prev_intent=prev_intent,
                rewritten_query=query # 追问不改写原文
            )

        # ====== 第4级：混合路由 ======
        # 打分两次：
        #   · 主判 scored（k=top_k 截断窗口）——主判过滤、澄清判据、仲裁、
        #     LLM 兜底近失候选四件事共用；路由输入用剥离路径实体的文本
        #     （routing_text），LLM 兜底仍看原文（模型对路径鲁棒，且改写
        #     契约基于原文）。
        #   · 拆分 rescored（k=路由数全量重扫）——只在业务分支的拆分判定里
        #     现打，动机与口径见 _split_steps。
        scored = self.router.scores(routing_text(query, entities),
                                    k=self.router.top_k)

        # 主判过滤：过各自生效阈值（路由专属优先，全局 None = 无门槛恒过）
        passed = [(name, score) for name, score in scored
                  if self._passes(name, score)]

        # 出口1：top_k 的意图都没有通过阈值
        if not passed:
            return await self._resolve_unmatched(query, entities,
                                                 prev_intent, scored)

        top_name, top_score = passed[0] # 分数最高的意图

        # 出口2:分数最高的意图是闲聊/超范围这类系统意图：直接产出单意图，不澄清也不拆——
        # 这类输入要么轻回复要么明确拒绝，不存在「在意图间取舍」的问题
        if not _is_business(top_name):
            return self._router_intent(top_name, top_score, entities,
                                       prev_intent, query)

        # 出口3:分数最高的意图是业务意图，进入业务意图的处理流程
        return await self._resolve_business(top_name, top_score, query,
                                            entities, prev_intent, scored)

    # ------------------------------------------------------------------
    # 第 4 级子判定：拆分 / 单业务消解 / 未命中消解
    # ------------------------------------------------------------------

    def _router_intent(self, name, score, entities, prev_intent, query,
                       extra_intents=None) -> IntentOutput:
        """路由层直接产出的意图结果（extra_intents 非空 = 复合句短路）。

        Args:
            name: 胜出意图的路由名（枚举值）。
            score: 该意图的融合分数（截断到 [0,1] 后作置信度）。
            entities: 第 1 级实体提取的产出，原样透传。
            prev_intent: 上一轮意图（追问链路审计用）。
            query: 用户原始输入，原样作为 rewritten_query（路由层不改写）。
            extra_intents: 拆分出的后续意图 [(路由名, 融合分数)]（保序、不含主意图）；
                None/空 = 单意图。

        Returns:
            source=ROUTER 的 IntentOutput，无澄清。
        """
        units = [IntentUnit(intent_type=IntentType(name),
                            confidence=self._clip01(score))]
        for extra_name, extra_score in (extra_intents or []):
            units.append(IntentUnit(intent_type=IntentType(extra_name),
                                    confidence=self._clip01(extra_score)))
        return IntentOutput(
            intents=units, entities=entities, source=IntentStep.ROUTER,
            prev_intent=prev_intent, rewritten_query=query)

    @staticmethod
    def _collect_extra_intents(top_name, rescored,
                               threshold_of, epsilon) -> list[tuple[str, float]]:
        """从重扫结果里挑出够格的后续意图（保序、不含主意图、无长度上限）。

        判据：跳过主意图与非业务意图；该路由必须标定过拆分阈值（>0，未标定的
        「过线」没有含金量）；分数需超过「自身阈值 + ε」。按重扫顺序返回
        (路由名, 分数)，分数保留供上层写成 per-intent 置信度。

        独立的静态方法而非内联：让「无长度上限」这条性质能被单测直接钉住。

        Args:
            top_name: 主判胜出的业务路由名（列表首项的固定起点，不算后续）。
            rescored: 第二次全量重扫的打分结果（已按分数降序）。
            threshold_of: 取某条路由拆分阈值的回调（None/≤0 视为未标定）。
            epsilon: 擦线保护余量。

        Returns:
            够格的后续意图 [(路由名, 融合分数)]，保序且不含主意图。
        """
        out: list[tuple[str, float]] = []
        for name, score in rescored:
            # 跳过主意图与非业务意图（chitchat/out_of_scope/help 不派发，
            # 拆进去只会让 spawn 门禁拒掉整条链）
            if name == top_name or not _is_business(name):
                continue
            threshold = threshold_of(name)
            # None 或 ≤0 → 这条路由"没标定过"，过线毫无含金量
            if threshold is None or threshold <= 0.0:
                continue
            if score >= threshold + epsilon:
                out.append((name, score))
        return out

    def _split_steps(self, top_name, top_score, query, entities,
                     prev_intent) -> IntentOutput | None:
        """多标签拆分：复合句在路由层直接拆出有序意图列表短路返回。

        第二意图候选要同时满足三个条件才能拆进列表：
        ① 它自己这条路由的阈值是 fit 标定过的（> 0）。routes.yaml 出厂时
           阈值全是 0.0，此时「过线」毫无含金量——未标定状态下任何第二
           高分都能过一条 0.0 的线，整句会被拆得面目全非。所以标定之前
           路由层不拆分，行为与旧版单标签完全一致；fit 写回真实阈值后
           拆分才自然激活。
        ② 分数超过「拆分阈值 + ROUTER_STEPS_EPSILON」——与主意图同一条
           规则，独立裁决在重扫分数上（业界多标签惯例：全类打分 + 逐类
           阈值，无第二名的特殊放宽）。
        ③ 是可派发的业务意图：闲聊、帮助这类系统意图永远不该出现在列表里
           ——它们不派发，拆进去只会让 spawn 门禁拒掉整条链。

        候选打分用第二次 scores()（k=路由数全量重扫）：主管道 top_k 截断
        只看分数最高的前几条例句，复合句里第二意图的例句常常排不进窗口、
        只能拿哨兵分——它根本没有候选资格，过线检查对它形同虚设。注意：
        scores(k) 的 k 数的是例句条数：以路由数为窗口（随新增意图自动增长）
        保证每个路由至少一个例句的曝光位。若把窗口放大到「全部例句」（字面
        意义的全类打分），路由分会变成全库均值，聚合口径改变、现有阈值随之
        失配——须先重新标定阈值再切。

        拆不出 2 个意图时返回 None，调用方继续单意图消解（仲裁 → 澄清 →
        快路径）。

        Args:
            top_name: 主判胜出的业务路由名（列表首项的固定起点）。
            top_score: 主意图的融合分数（透传为列表首项的置信度）。
            query: 用户原始输入（重扫仍用剥离实体后的文本，由本方法内部处理）。
            entities: 第 1 级实体提取的产出，透传给结果。
            prev_intent: 上一轮意图，透传给结果。

        Returns:
            拆出 ≥2 个意图时返回 source=ROUTER、意图列表非空的 IntentOutput
            （主意图是第一项）；否则 None。
        """
        # 1. 全量重扫打分
        # 为什么需要第二次打分：主判打分只看最像的前 3 条例句，
        # 复合句里第二意图的例句往往排不进前 3——它拿到的是哨兵分（-1e9），等于"没被看见"，后面的过线检查对它形同虚设。
        # 参数 k = 路由数
        rescored = self.router.scores(routing_text(query, entities),
                                      k=len(self.router.get_thresholds()))

        # 2. 收集后续意图（保序、不含主意图、无长度上限）。
        # 判据与重扫顺序见 _collect_extra_intents；拆分阈值查询链：
        # steps_threshold（重扫口径单独标定的值）→ 没标定则回落主判
        # score_threshold（安全默认）→ 都没有则 None。
        # rescored 已按分数降序，所以拆出来的列表天然按"自信程度"排序。
        extra = self._collect_extra_intents(
            top_name, rescored,
            self.router.get_steps_threshold, ROUTER_STEPS_EPSILON)

        # 3. 没有任何后续意图 → query 不是复合意图，返回 None
        if not extra:
            return None

        # 4. 至少两个业务意图都过线 → query 是一句复合请求，直接在路由层拆开短路返回，不进 LLM 兜底。
        # 主意图是列表第一项，完整意图列表随 INTENT 块注入，作收尾核对的事实来源。
        return self._router_intent(top_name, top_score, entities,
                                   prev_intent, query,
                                   extra_intents=extra)

    async def _resolve_business(self, top_name, top_score, query, entities,
                                prev_intent, scored) -> IntentOutput:
        """业务意图的消解瀑布：拆分 → 仲裁 → 澄清 → 快路径。

        ① 拆分：复合句在路由层拆开短路（见 _split_steps）；
        ② 仲裁：第二名业务候选咬得很近（分差 < ROUTER_ARBITRATION_MARGIN）
           时，路由器没把握，让 LLM 结合意图定义二选一；失败回落 ③；
        ③ 澄清：分数贴线（S1）或竞争（S2）时问用户；问不问由代码判据定；
        ④ 快路径：又准又稳直接产出单意图——绝大多数输入走这里，零 LLM 调用。

        Args:
            top_name: 主判胜出的业务路由名。
            top_score: 主意图的融合分数（快路径结果的 confidence）。
            query: 用户原始输入。
            entities: 第 1 级实体提取的产出，透传给结果。
            prev_intent: 上一轮意图，透传给澄清轮与结果。
            scored: 主判分数（降序），仲裁与澄清判据共用。

        Returns:
            四个出口之一的 IntentOutput（source = ROUTER / LLM，澄清轮 =
            LLM 且 clarification 非空）。
        """
        # 1. 尝试拆分出多意图
        split = self._split_steps(top_name, top_score, query, entities,
                                  prev_intent)

        # 1.1 query 是复合意图，返回多项意图列表的 IntentOutput
        if split is not None:
            return split

        # 2. 未拆分出多意图，判断前两名业务意图的分数是否很接近
        candidates = self._near_contested(scored)

        # 2.1 前两名业务意图的分数很接近，因此需要让 LLM 从前两名业务意图进行二选一（第一步已经否认了 query 是多意图）
        if candidates is not None:
            arbitrated = await self._arbitrate(query, entities, candidates) # 仲裁结果
            if arbitrated is not None:
                return arbitrated

        # 3. query 不含多意图，而且前两名业务意图的分数有差距（路由器对分数最高的意图有把握），
        # 判断是否需要向用户提出澄清，两条"不自信"信号任一成立即问：
        #   · S1 贴线：第一名分数 < 自身阈值 + δ——刚压线过，硬选大概率错；
        #   · S2 竞争：前两名分差 < 0.15——与仲裁共用同一条线，所以走到这里的只有"仲裁失败且两候选咬得极近"的场景（问用户兜住仲裁的失败）。
        if self._ambiguous(scored):
            return await self._clarify_round(query, entities, prev_intent,
                                             scored)

        # 4. 直接产出单意图
        return self._router_intent(top_name, top_score, entities,
                                   prev_intent, query)

    async def _resolve_unmatched(self, query, entities, prev_intent,
                                 scored) -> IntentOutput:
        """路由全未命中的消解：先过澄清判据，再落 LLM 兜底。

        未命中不代表没有候选——scored 里还有没过线的「近失」意图。若近失
        候选里有业务意图且分数贴线/互相咬近，说明用户输入处在几个意图的
        模糊地带，值得问一句；否则才真正交给 LLM 兜底盲解析。

        Args:
            query: 用户原始输入（LLM 兜底看原文，不看剥离后的路由文本）。
            entities: 第 1 级实体提取的产出，透传给结果。
            prev_intent: 上一轮意图，透传给澄清轮与结果。
            scored: 主判分数（降序），澄清判据与 LLM 近失候选共用。

        Returns:
            澄清轮（source=LLM，clarification 非空）或 LLM 常规解析
            （source=LLM，clarification 恒为 None）的 IntentOutput。
        """
        # 判断是否需要向用户提出澄清
        if self._ambiguous(scored):
            return await self._clarify_round(query, entities, prev_intent,
                                             scored)

        # ====== 第5级：LLM 兜底（常规解析） ======
        # 走到这里说明代码判据认为「不需要澄清」。但 LLM 拿到输入后仍可能自作主张产出 clarification——
        # 一律丢弃：澄清的「问不问」只认代码判据，否则等于又把触发权交回给模型心证。
        # 复合拆分照常透传：LLM 拆分是复合意图在第四级时没有成功拆分后的第二个兜底，
        # 触发契约在提示词里约定；拆了还要问的违命输出由 schema 护栏清掉 clarification。
        result = await self._llm_extract(query, scored, force_clarification=False)
        # 扁平 LLM 结果 → 意图列表：主意图取模型给的类型与概率，后续步骤没有对应
        # 分数（置 None，不编造），保序去重并跳过与主意图重复的。
        units = [IntentUnit(intent_type=result.intent_type,
                            confidence=result.confidence)]
        seen = {result.intent_type}
        for step in result.steps:
            if step not in seen:
                seen.add(step)
                units.append(IntentUnit(intent_type=step, confidence=None))
        return IntentOutput(
            intents=units,
            entities=entities, source=IntentStep.LLM, prev_intent=prev_intent,
            rewritten_query=result.query_rewrite or query, # 若 LLM 提供了改写则用，否则保留原文
            clarification=None,
        )

    # ------------------------------------------------------------------
    # 路由判定辅助
    # ------------------------------------------------------------------

    def _effective_threshold(self, name: str) -> float | None:
        """路由生效阈值：路由专属优先，否则全局；全局也未设则 None（无门槛恒过）。

        与 router._pass_routes 的阈值选取逻辑保持一致。

        Args:
            name: 路由名（枚举值）。

        Returns:
            生效阈值；整条链都未设时返回 None。
        """
        route = self.router.get(name)
        if route is not None and route.score_threshold is not None:
            return route.score_threshold
        return self.router.score_threshold

    def _passes(self, name: str, score: float) -> bool:
        """单路由阈值裁决：阈值未设恒过，否则 score >= 阈值。

        Args:
            name: 路由名（枚举值）。
            score: 该路由的融合分数。

        Returns:
            True 表示该候选通过主判过滤。
        """
        threshold = self._effective_threshold(name)
        return True if threshold is None else score >= threshold

    def _near_contested(self, scored: list[tuple[str, float]]) -> list[IntentType] | None:
        """边界仲裁触发判定：top-2 业务候选分差小于仲裁线时返回这两个候选。

        只看业务意图（可派发的才有选错方向的硬代价），且要求第一名过线——
        本判定只在单业务命中的分支里被调用。分差够大（路由器有把握）返回 None。

        Args:
            scored: 主判分数 [(路由名, 融合分数)]，按分数降序。

        Returns:
            触发时返回 [候选A, 候选B]（按分数降序的 IntentType，长度恒 2）；
            候选不足两个或分差 ≥ ROUTER_ARBITRATION_MARGIN 时返回 None。
        """
        # 1. 从主判分数里筛出"业务候选"
        # scored 是主判分数（降序），所以 biz 也保持降序——biz[0] 是业务意图里的第一名，biz[1] 是第二名。
        biz = [(name, score) for name, score in scored
               if score > 0 and _is_business(name)]

        # 2. 业务候选不足两个或前两名业务意图分差大于设定的阈值——返回 None：路由器有把握，不需要 LLM 去仲裁
        if len(biz) < 2 or biz[0][1] - biz[1][1] >= ROUTER_ARBITRATION_MARGIN:
            return None

        # 3. 前两名业务意图分差小于设定的阈值——返回按分数降序的两个候选（IntentType 枚举），
        # 调用方 _arbitrate 会把它们连同意图定义塞进 prompt 让 LLM 二选一。
        return [IntentType(biz[0][0]), IntentType(biz[1][0])]

    async def _arbitrate(self, query: str, entities: dict,
                         candidates: list[IntentType]) -> IntentOutput | None:
        """边界仲裁：让 LLM 在两个贴近的业务候选里二选一。

        分工与澄清轮同构——「选谁」由路由分差圈定候选、LLM 在候选内表态，
        越出候选的选择一律作废；任何失败（异常/非法输出）返回 None，调用方
        回落到澄清判据或快路径，绝不因仲裁故障打断主流程。intent_type 取
        LLM 的选择（source=LLM，审计可辨），confidence 用模型自报把握。

        Args:
            query: 用户原始输入（放进仲裁 prompt 的「用户请求」栏）。
            entities: 第 1 级实体提取的产出，透传给结果。
            candidates: 待仲裁的两个业务候选（_near_contested 的产出）。

        Returns:
            仲裁成功返回 source=LLM、单意图的 IntentOutput；LLM 异常或
            选择越出候选时返回 None（调用方回落）。
        """
        def label(t: IntentType) -> str:
            """意图的展示标签：中文短名 + 枚举值（写入仲裁 prompt 的候选行）。

            Args:
                t: 候选意图枚举值。

            Returns:
                「中文名(枚举值)」形式的标签字符串。
            """
            return f"{INTENT_LABELS_ZH.get(t, t.value)}({t.value})"

        prompt = (
            "你是意图识别仲裁器。路由器对一个用户请求给出了两个难以取舍的候选意图，"
            "请你根据用户的话二选一。\n\n"
            f"用户请求：{query}\n\n"
            f"候选A：{label(candidates[0])}\n候选B：{label(candidates[1])}\n\n"
            "只输出 JSON：{\"intent_type\": <候选枚举值>, \"confidence\": <0到1>}"
        )
        try:
            result = await self.structured.extract(prompt, ArbitrationChoice)
        except Exception:
            return None
        if result.intent_type not in candidates:
            return None
        return IntentOutput(
            intents=[IntentUnit(intent_type=result.intent_type,
                                confidence=self._clip01(result.confidence))],
            entities=entities, source=IntentStep.LLM, rewritten_query=query,
        )

    def _clip01(self, score: float) -> float:
        """融合分数截断到 [0,1]（cosine 可为负、稀疏点积可 >1，非概率）。

        Args:
            score: 融合分数。

        Returns:
            截断到 [0,1] 区间内的分数。
        """
        return max(0.0, min(1.0, score))

    def _ambiguous(self, scored: list[tuple[str, float]]) -> bool:
        """判断当前输入是否值得向用户澄清——委托模块级 is_ambiguous
        （S1/S2 判据与判定表见其 docstring）。

        Args:
            scored: 主判分数 [(路由名, 融合分数)]，按分数降序。

        Returns:
            True 表示该向用户澄清（触发强制澄清轮）。
        """
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
          链路退化成永远不触发的死路径。
        - 意图与置信度照常产出不缺席：澄清是搭在正常识别结果上的附加通道，
          不改变单标签答案。

        单意图列表 + 澄清是本轮的正常形态：澄清和拆分互斥——都要拆了就不需要问，
        都要问了就别拆；列表长度 ≥2 时澄清会被 schema 护栏清掉。

        候选回传锚点：业务候选 top2 写入
        clarify_candidates，问题末尾由代码追加编号选项行（format_intent_options）
        ——文案权在模型、选项枚举权在代码：散文式提问无法保证可解析，编号行
        保证用户回复经 match_option_choice 确定性解析回意图，落地为
        会话意图（runtime 在 run 内同步问、代码级落地，无跨轮状态）。

        Args:
            query: 用户原始输入（LLM 生成澄清文案时看原文）。
            entities: 第 1 级实体提取的产出，透传给结果。
            prev_intent: 上一轮意图，透传给结果。
            scored: 主判分数（降序），供 LLM prompt 近失候选与模板合成取前两名。

        Returns:
            source=LLM、clarification 非空的 IntentOutput（意图列表恒为单意图）。
        """
        candidates = [IntentType(name) for name, score in scored
                      if score > 0 and _is_business(name)][:2]
        result = await self._llm_extract(query, scored, force_clarification=True)
        clarification = result.clarification or self._synthesize_clarification(scored)
        if candidates:
            clarification = f"{clarification}\n{format_intent_options(candidates)}"
        return IntentOutput(
            intents=[IntentUnit(intent_type=result.intent_type,
                                confidence=result.confidence)],  # 澄清轮不拆分
            entities=entities, source=IntentStep.LLM, prev_intent=prev_intent,
            rewritten_query=result.query_rewrite or query,
            clarification=clarification,
            clarify_candidates=candidates,
        )

    async def _llm_extract(self, query: str, scored: list[tuple[str, float]],
                           force_clarification: bool) -> IntentionResult:
        """调结构化输出模块做 LLM 兜底，注入路由近失候选（top-k 融合分数）。

        Args:
            query: 用户原始输入。
            scored: 主判分数（降序），作为近失候选写进 prompt。
            force_clarification: 是否强制澄清轮（切换 prompt 里的 clarification 条款）。

        Returns:
            LLM 结构化输出的 IntentionResult；调用失败时为 UNCLASSIFIED 兜底值。
        """
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
        原样打给用户，枚举英文值用户看不懂。

        Args:
            scored: 主判分数 [(路由名, 融合分数)]，按分数降序。

        Returns:
            合成的澄清问题文本（永不为空）。
        """
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
                "step 是一个业务意图名（可派发类），按执行顺序排列；单一动作或拿不准时"
                "必须留空（宁可不拆）。拆分时 intent_type 取第一步。",
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
