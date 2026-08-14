# paperflow/core/intent/schemas/intent.py
"""意图识别输出契约——识别管线各阶段产出的统一数据结构。

这是"输出契约"：定义识别结果的形态，与 schemas/route.py 的"路由契约"（路由器
输入/输出）职责不同。识别管线分四级：实体提取 / 追问检测 / 混合路由 /
LLM 兜底，本模块定义的几个类型是它们共同使用的产出契约：

- ``IntentType``: 14 类意图枚举（枚举值即路由名，对应 routes.yaml 的 route 名集合）。
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
    14 值按三类组织（category 见 INTENT_META），类别是消费分组不是路由层级。
    """

    SET_RESEARCH_TOPIC = "set_research_topic"  # 设定研究方向（业务；记录+引导，不派发）
    SEARCH_PAPER = "search_paper"              # 搜索/查找论文（业务；槽位 query/source/year/download）
    ASK_QUESTION = "ask_question"              # 具体问答（业务）
    GENERATE_NOTE = "generate_note"            # 撰写笔记（业务）
    WRITE_OUTLINE = "write_outline"            # 撰写研究大纲（业务）
    ANALYZE_PAPER = "analyze_paper"            # 精读/分析论文（业务）
    MANAGE_MEMORY = "manage_memory"            # 记忆查询 + 待读清单操作（业务）
    REFINE_QUERY = "refine_query"              # 修正上轮查询（对话管理；重派入口）
    SWITCH_TOPIC = "switch_topic"              # 切换研究方向（对话管理；记忆归档，不派发）
    CHITCHAT = "chitchat"                      # 闲聊（系统；直接回复）
    OUT_OF_SCOPE = "out_of_scope"              # 超出能力范围（系统；明确拒绝）
    HELP = "help"                              # 帮助/功能引导（系统）
    FEEDBACK = "feedback"                      # 结果反馈（系统；记忆日志）
    GENERAL = "general"                        # 兜底：路由未命中 / LLM 解析失败（系统）


class IntentCategory(str, Enum):
    """意图类别——消费分组（三类组织），非路由层级。"""

    BUSINESS = "business"        # 业务：派发领域 agent 或记忆操作
    DIALOGUE = "dialogue"        # 对话管理：会话状态操作（继承/归档/重派）
    SYSTEM = "system"            # 系统：直接回复，永不 spawn


#: 意图 → (category, dispatch_allowed)——单一真相源。枚举=契约=实现集：
#: 14 值全覆盖、无悬空；dispatch_allowed=False 的意图由 spawn 门禁代码级拒绝派发
#: （set_research_topic 是业务但非派发——记录+引导；refine_query 是对话管理但派发——重派入口）。
INTENT_META: dict[IntentType, tuple[IntentCategory, bool]] = {
    IntentType.SET_RESEARCH_TOPIC: (IntentCategory.BUSINESS, False),
    IntentType.SEARCH_PAPER:       (IntentCategory.BUSINESS, True),
    IntentType.ASK_QUESTION:       (IntentCategory.BUSINESS, True),
    IntentType.GENERATE_NOTE:      (IntentCategory.BUSINESS, True),
    IntentType.WRITE_OUTLINE:      (IntentCategory.BUSINESS, True),
    IntentType.ANALYZE_PAPER:      (IntentCategory.BUSINESS, True),
    IntentType.MANAGE_MEMORY:      (IntentCategory.BUSINESS, True),
    IntentType.REFINE_QUERY:       (IntentCategory.DIALOGUE, True),
    IntentType.SWITCH_TOPIC:       (IntentCategory.DIALOGUE, False),
    IntentType.CHITCHAT:           (IntentCategory.SYSTEM, False),
    IntentType.OUT_OF_SCOPE:       (IntentCategory.SYSTEM, False),
    IntentType.HELP:               (IntentCategory.SYSTEM, False),
    IntentType.FEEDBACK:           (IntentCategory.SYSTEM, False),
    IntentType.GENERAL:            (IntentCategory.SYSTEM, False),
}


class IntentStep(str, Enum):
    """产出阶段枚举——让审计/监控能看出意图由哪一级产出。"""

    ENTITIES = "entities"                  # 实体提取阶段
    FOLLOWUP = "followup"                  # 追问检测阶段（依赖会话上下文）
    ROUTER = "router"                      # 混合路由阶段
    LLM = "llm"                            # LLM 兜底阶段


class IntentOutput(BaseModel):
    """管线逐级产出的结构化意图。

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

    #: 复合意图的有序拆分（LLM 兜底阶段填充；路由命中时保持为空——单意图无需拆分）
    steps: list["IntentType"] = []

    #: 歧义澄清问题（LLM 兜底阶段填充；非空时管线提前返回，由调用方跨轮挂起待澄清意图）
    clarification: str | None = None

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

    #: 复合意图的有序拆分（供后续扩展；若结构化输出契约缺该字段，LLM 兜底无法产出拆分结果）
    steps: list["IntentType"] = []

    #: 歧义澄清问题（非空时管线提前返回，由调用方跨轮挂起待澄清意图）
    clarification: str | None = None
