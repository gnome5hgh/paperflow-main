# paperflow/core/intent/schemas/intent.py
"""意图识别输出契约——识别管线各阶段产出的统一数据结构。

这是"输出契约"：定义识别结果的形态，与 schemas/route.py 的"路由契约"（路由器
输入/输出）职责不同。识别管线分五级：实体提取 / 选项答复检测 / 追问检测 / 混合路由 /
LLM 兜底，本模块定义的几个类型是它们共同使用的产出契约：

- ``IntentType``: 13 类意图枚举（枚举值即路由名，对应 routes.yaml 的 route 名集合）。
- ``IntentCategory``: 意图类别（business/dialogue/system）——消费分组，非路由层级。
- ``INTENT_META``: intent → (category, dispatch_allowed) 单一真相源映射。
- ``IntentStep``: 产出阶段枚举——审计/监控据此区分"这条意图是路由层定的
  还是 LLM 兜底定的"（从而统计路由命中率、LLM 兜底率）。
- ``IntentOutput``: 管线逐级产出的结构化意图，供上层调用方直接消费。
- ``IntentionResult``: LLM 兜底阶段的结构化输出契约，扁平三字段，
  不封装路由器的内部决策。
"""

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from paperflow.core.security.text import sanitize_surrogates


class IntentType(str, Enum):
    """意图类型枚举，value 与路由名一致（routes.yaml 中的 name）。

    枚举 = 契约 = 当前实现集——不允许"枚举允许但系统无处理路径"的悬空值。
    13 值按三类组织（category 见 INTENT_META），类别是消费分组不是路由层级。
    历史收敛：switch_topic 并入 set_research_topic、refine_query 并入
    search_paper——两者与近邻意图的边界是对话史信号，路由器原理上不可学，
    且派发行为与保留值完全一致。
    """

    SET_RESEARCH_TOPIC = "set_research_topic"  # 研究 topic 管理：设定/切换（业务；记录+归档，不派发）
    MENU_SELECTION = "menu_selection"          # 菜单选项答复（对话管理；选择动作不重分类，派发权在 supervisor 对照菜单）
    SEARCH_PAPER = "search_paper"              # 搜索/查找论文（业务；含对上轮检索的修正重搜；槽位 query/source/year/download）
    ASK_QUESTION = "ask_question"              # 具体问答：即问即答的单点问题（业务）
    GENERATE_NOTE = "generate_note"            # 撰写笔记（业务）
    RESEARCH_DISCOVERY = "research_discovery"  # 选题发现：交付方向/课题建议（业务；搜文献只是其手段）
    ANALYZE_PAPER = "analyze_paper"            # 精读分析：交付分析报告的长任务（业务）
    MANAGE_MEMORY = "manage_memory"            # 记忆查询 + 待读清单操作（业务）
    CHITCHAT = "chitchat"                      # 闲聊与应答语（系统；直接回复）
    OUT_OF_SCOPE = "out_of_scope"              # 超出能力范围：含与论文工作无关的请求（系统；明确拒绝）
    HELP = "help"                              # 本系统的使用方法/功能引导（系统）；系统无关请求归 out_of_scope
    FEEDBACK = "feedback"                      # 结果反馈（系统；记忆日志）
    UNCLASSIFIED = "unclassified"              # 未分类兜底：路由未命中 / LLM 解析失败（系统）。仅 LLM 兜底产出，不在路由知识库


class IntentCategory(str, Enum):
    """意图类别——消费分组（三类组织），非路由层级。"""

    BUSINESS = "business"        # 业务：派发领域 agent 或记忆操作
    DIALOGUE = "dialogue"        # 对话管理：会话状态操作（继承/归档/重派）
    SYSTEM = "system"            # 系统：直接回复，永不 spawn


# 意图 → (category, dispatch_allowed)——单一真相源。枚举=契约=实现集
# dispatch_allowed=False 的意图由 spawn 门禁代码级拒绝派发
INTENT_META: dict[IntentType, tuple[IntentCategory, bool]] = {
    IntentType.SET_RESEARCH_TOPIC: (IntentCategory.BUSINESS, False), # set_research_topic 是业务但非派发——记录+引导
    IntentType.MENU_SELECTION:     (IntentCategory.DIALOGUE, True), # menu_selection 是对话管理但派发——选择动作，派发权在 supervisor 对照菜单
    IntentType.SEARCH_PAPER:       (IntentCategory.BUSINESS, True),
    IntentType.ASK_QUESTION:       (IntentCategory.BUSINESS, True),
    IntentType.GENERATE_NOTE:      (IntentCategory.BUSINESS, True),
    IntentType.RESEARCH_DISCOVERY: (IntentCategory.BUSINESS, True),
    IntentType.ANALYZE_PAPER:      (IntentCategory.BUSINESS, True),
    IntentType.MANAGE_MEMORY:      (IntentCategory.BUSINESS, True),
    IntentType.CHITCHAT:           (IntentCategory.SYSTEM, False),
    IntentType.OUT_OF_SCOPE:       (IntentCategory.SYSTEM, False),
    IntentType.HELP:               (IntentCategory.SYSTEM, False),
    IntentType.FEEDBACK:           (IntentCategory.SYSTEM, False),
    IntentType.UNCLASSIFIED:       (IntentCategory.SYSTEM, False),
}


#: 意图 → 中文短标签。用于两类面向用户的场合：澄清模板合成兜底问题（枚举英文值
#: 用户看不懂）、日志/展示层。只求自足易懂，不复述枚举注释里的完整判据。
INTENT_LABELS_ZH: dict[IntentType, str] = {
    IntentType.SET_RESEARCH_TOPIC: "设定/切换研究方向",
    IntentType.MENU_SELECTION:     "菜单选项选择",
    IntentType.SEARCH_PAPER:       "搜索论文",
    IntentType.ASK_QUESTION:       "论文问答",
    IntentType.GENERATE_NOTE:      "撰写笔记",
    IntentType.RESEARCH_DISCOVERY: "选题发现",
    IntentType.ANALYZE_PAPER:      "精读分析",
    IntentType.MANAGE_MEMORY:      "记忆/清单管理",
    IntentType.CHITCHAT:           "闲聊",
    IntentType.OUT_OF_SCOPE:       "超出能力范围的请求",
    IntentType.HELP:               "使用帮助",
    IntentType.FEEDBACK:           "结果反馈",
    IntentType.UNCLASSIFIED:       "未分类",
}


#: 复合拆分步数上限（唯一定义点，契约层）。
#: - 值：3。
#: - 含义与单位：IntentionResult.steps 允许的最大长度，也用于多标签拆分截断（步数，整数）。
#: - 改它的后果：改变多标签拆分与 steps 护栏口径；
#:   _steps_guard 与两处提示词文案（下方 Field description、pipeline._build_llm_prompt）
#:   均与本常量同源插值。
#: - 是否进 YAML：否（结构契约常量）。
#: 定在 schemas 而非 constants.py：pipeline 依赖 schemas，放 constants 会成循环导入。
MAX_STEPS = 3


class IntentStep(str, Enum):
    """产出阶段枚举——让审计/监控能看出意图由哪一级产出。"""

    ENTITIES = "entities"                  # 实体提取阶段
    OPTION = "option"                      # 选项答复检测阶段（纯编号菜单选择，确定性正则）
    FOLLOWUP = "followup"                  # 追问检测阶段（依赖会话上下文）
    ROUTER = "router"                      # 混合路由阶段
    LLM = "llm"                            # LLM 兜底阶段
    USER = "user"                          # 用户确认阶段——澄清回路的用户选择代码级落地
                                           # （管线澄清编号选择 / ask_user 带 intent_options），
                                           # 不经路由器复判（同一句话复判只会复现同一误判）


class IntentOutput(BaseModel):
    """pipeline 管线逐级产出的结构化意图。

    confidence 语义（两种来源，消费方按 source 区分解释）：
    ROUTER 来源 = 融合分数 clip 到 [0,1]（非概率，可为边缘值）；LLM 来源 = 模型概率。
    """

    #: 意图类型
    intent_type: IntentType

    #: 置信度，范围约束在 [0,1]（LLM 可能输出越界值，pydantic 强制约束）
    confidence: float = Field(ge=0.0, le=1.0)

    #: 实体提取阶段得到的实体（由管线填充）
    entities: dict = Field(default_factory=dict)

    #: 管线输入原文；LLM 兜底改写时为其改写结果（缺省为原文）
    rewritten_query: str = ""

    #: 产出阶段——审计/监控可观测
    source: IntentStep

    #: 上一轮意图（追问检测阶段填充，来自会话上下文）
    prev_intent: IntentType | None = None

    #: 复合意图的有序拆分（LLM 兜底或路由 multi-label 命中 ≥2 时填充；单意图为空）
    steps: list["IntentType"] = []

    #: 歧义澄清问题（LLM 兜底阶段填充；非空时管线提前返回，由调用方跨轮挂起待澄清意图）
    clarification: str | None = None

    #: 澄清的候选意图（仅 clarification 非空时填充，业务候选 top2，按展示顺序）。
    #: 澄清回路的代码级回传锚点：CLI 挂起澄清时带走，用户回复经
    #: routing.confirm.match_option_choice 解析回其中之一，直接落地会话意图——
    #: 不进 INTENT 块（对模型是噪声），_intent_block 序列化时排除。
    clarify_candidates: list["IntentType"] = []

    @model_validator(mode="after")
    def _sanitize_surrogates(self) -> "IntentOutput":
        """清洗未配对的 surrogate 字符（PDF 提取 / LLM 兜底输出可能携带）。

        若不清洗，后续 model_dump_json 会抛 PydanticSerializationError
        （实测输入如 '将上面内容总结为笔记' 可能触发 '\\udce5' 报错）。
        与 security.text 的信任边界清洗思路一致：这是跨管线消费的契约类型，
        在构造时兜住脏文本，上游调用方无需逐个清洗。"""
        self.rewritten_query = sanitize_surrogates(self.rewritten_query)
        if self.clarification:
            self.clarification = sanitize_surrogates(self.clarification)
        if self.entities:
            self.entities = {
                k: sanitize_surrogates(v) if isinstance(v, str) else v
                for k, v in self.entities.items()
            }
        return self


class IntentionResult(BaseModel):
    """LLM 兜底阶段的结构化输出契约。

    扁平三字段：底层结构化输出机制只校验类型不校验数值范围，
    因此 confidence 仍保留 pydantic 范围约束，避免 LLM 越界值流入上游。
    """

    #: 意图类型
    intent_type: IntentType

    #: 置信度（模型概率，范围约束 [0,1]）
    confidence: float = Field(ge=0.0, le=1.0)

    #: LLM 改写后的查询（缺省为空串，管线使用原文）
    query_rewrite: str = ""

    #: 复合意图的有序拆分。description 会经 StructuredOutput 展开进提示词，是模型
    #: 判断「何时拆」的唯一依据（同 clarification 的教训——缺了它 steps 永远为空）。
    #: 填写条件：仅当输入包含 ≥2 个相互独立、分属不同
    #: 意图的业务动作；每个 step 必须是单业务意图（dispatch_allowed=True），按执行
    #: 顺序排列，最多 MAX_STEPS 步，且 steps[0] 必须等于 intent_type；单一动作或拿不准时
    #: 必须留空（宁缺勿滥——steps 非空会放行 spawn 门禁，误拆即高权限口子）。
    #: 注意：# 注释不会进入 pydantic description——触发契约
    #: 必须走下面的 Field(description=...) 才能进 LLM 提示词，这里仅留出处索引。
    steps: list["IntentType"] = Field(
        default=[],
        description=(
            "复合意图的有序拆分，仅在输入包含 ≥2 个相互独立、分属不同意图的业务动作时填写；"
            "每个 step 必须是单业务意图（dispatch_allowed=True 的枚举值），按执行顺序排列，"
            f"最多 {MAX_STEPS} 步，且 steps[0] 必须等于 intent_type；单一动作或拿不准时必须留空"
            "（宁缺勿滥——steps 非空会放行 spawn 门禁，误拆即高权限口子）。"
        ),
    )

    @model_validator(mode="after")
    def _steps_guard(self) -> "IntentionResult":
        """steps 合法性护栏 + steps 与 clarification 互斥（代码级防御）。

        steps 里的每一项都会被 spawn 门禁当作可派发意图放行，所以这里必须在
        schema 层拦住三类非法拆分：超过 MAX_STEPS 步（LLM 硬凑的长链）、混入非派发意图
        （LLM 把闲聊/帮助也拆进去，等于给不派发的意图开派发口子）、首步与主意图
        不一致（主意图是 spawn 门禁和 INTENT 块的第一参考，错位会让两者打架）。
        违规不做半截修正，整体置空；也不抛校验错误——解析失败的兜底路径
        （fallback=UNCLASSIFIED）不该因护栏再炸一次。
        互斥：steps 非空说明输入已被拆解执行，无需再澄清；两者同时产出属模型
        违命，clarification 让位。两字段的「要不要」上游管线均已用代码判据决定，
        这里是最后一条防线。
        """
        if self.steps:
            business = {t for t, (_, allowed) in INTENT_META.items() if allowed}
            if (len(self.steps) > MAX_STEPS
                    or any(s not in business for s in self.steps)
                    or self.steps[0] != self.intent_type):
                object.__setattr__(self, "steps", [])
        # steps × clarification 互斥：复合句已拆就无需澄清，两者同时产出属模型
        # 违命，代码级强制 clarification 让位（不抛错，静默清空即可）
        if self.steps and self.clarification:
            object.__setattr__(self, "clarification", None)
        return self

    #: 歧义澄清问题（非空时管线提前返回，由调用方跨轮挂起待澄清意图）
    #: 这个 description 与上面的 #: 注释重复是有意的：#: 只给读代码的人看，
    #: description 会经 StructuredOutput 的 schema 展开进提示词，是模型判断
    #: 「什么时候该填这个字段」的依据——缺了它澄清几乎不会被产出。
    clarification: str | None = Field(
        default=None,
        description=(
            "输入意图不明确、无法在意图间取舍时，写给用户的一句简短澄清问题；"
            "能推断出合理意图时留空，用 intent_type 与 confidence 表达不确定程度。"
        ),
    )
