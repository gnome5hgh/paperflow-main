# paperflow/core/intent/intent_schema.py
"""意图识别输出契约（spec Section 4，Layer 1 定死，Layer 4 扩展）。

四级级联管线（Stage 0 实体提取 / Stage 1 追问检测 / Stage 2 HybridRouter /
Stage 3 LLM 兜底）的统一产出：

- ``IntentType``: 5 类意图枚举（value 即路由名，= routes.yaml 的 route 名集合）。
- ``IntentStep``: 产出阶段枚举——审计/监控据此区分"这条意图是 router 定的还是
  LLM 兜底的"（router 命中率、LLM 兜底率 = Stage 2 质量指标）。
- ``IntentOutput``: 管线逐级产出的结构化意图，Supervisor 直接消费。
- ``IntentionResult``: Stage 3 的 LLM 兜底 schema（ADR 0006 StructuredOutput 消费），
  扁平三字段，不封装路由器内部决策。
"""

from enum import Enum

from pydantic import BaseModel, Field


class IntentType(str, Enum):
    """意图类型枚举，value 与路由名一致（routes.yaml 中的 name）。

    枚举 = 契约 = 当前实现集——不允许"枚举允许但系统无处理路径"的悬空值。
    """

    SEARCH_PAPER = "search_paper"          # 搜索/查找论文（ADR 的 search/download 合并，参数 download 区分留 Layer 4）
    GENERATE_NOTE = "generate_note"        # 撰写笔记
    ASK_QUESTION = "ask_question"          # 具体问答（ADR 的 read/answer/query_notes 合并，mode 参数留 Layer 4）
    MANAGE_MEMORY = "manage_memory"        # 记忆查询（读过哪些/阅读记录）
    GENERAL = "general"                    # 兜底：路由未命中 / LLM 解析失败
    # READ_PAPER / QUERY_NOTES：Layer 4 细化时加入（届时同步扩 routes.yaml + 枚举）


class IntentStep(str, Enum):
    """产出阶段枚举——审计/监控可观测（spec 的 B 核心价值）。"""

    ENTITIES = "entities"                  # Stage 0 实体提取（Layer 4 实现）
    FOLLOWUP = "followup"                  # Stage 1 追问检测（Layer 4 实现，依赖 session）
    ROUTER = "router"                      # Stage 2 HybridRouter（真实）
    LLM = "llm"                            # Stage 3 LLM 兜底（真实）


class IntentOutput(BaseModel):
    """管线逐级产出的结构化意图。

    confidence 语义（两种来源，消费方按 source 区分解释）：
    ROUTER 来源 = 融合分数 clip 到 [0,1]（非概率，可为边缘值）；LLM 来源 = 模型概率。
    """

    #: 意图类型
    intent_type: IntentType

    #: 置信度范围约束（LLM 可能输出越界值，pydantic 强制 [0,1]）
    confidence: float = Field(ge=0.0, le=1.0)

    #: Stage 0 提取的实体（Layer 4 填充）
    entities: dict = Field(default_factory=dict)

    #: 管线输入原文 / Stage 3 LLM 改写（缺省原文）
    rewritten_query: str = ""

    #: 产出阶段——审计/监控可观测（spec 的 B 核心价值）
    source: IntentStep

    #: Stage 1 填充的前一意图（Layer 4，session 提供）
    prev_intent: IntentType | None = None

    #: 复合意图有序拆分（Stage 3 填；Stage 2 路由命中保持空——单意图交 Supervisor ReAct 推断）
    steps: list["IntentType"] = []

    #: 歧义澄清问题（Stage 3 填；非空时 run() 前置钩子提前返回，CLI 跨轮挂起 pending_intent）
    clarification: str | None = None


class IntentionResult(BaseModel):
    """Stage 3 的 LLM 兜底 schema（ADR 0006 StructuredOutput 消费）。

    扁平三字段：StructuredOutput 只校验类型不校验范围，故 confidence 仍保留
    pydantic 范围约束，避免 LLM 越界值流入上游。
    """

    #: 意图类型
    intent_type: IntentType

    #: 置信度（模型概率，范围约束 [0,1]）
    confidence: float = Field(ge=0.0, le=1.0)

    #: LLM 改写后的查询（缺省为空串，pipeline 回落原文）
    query_rewrite: str = ""

    #: 复合意图有序拆分（Stage 3 的 StructuredOutput schema——不带则 LLM 兜底无法产出）
    steps: list["IntentType"] = []

    #: 歧义澄清问题（同上；非空时 run() 前置钩子提前返回，CLI 跨轮挂起 pending_intent）
    clarification: str | None = None
