# paperflow/core/intent/intent_schema.py
"""
意图识别输出契约（ADR 0007）。

四级级联管线（Stage 0 预处理 / Stage 1 追问检测 / Stage 2 HybridRouter / Stage 3 LLM 兜底）
的统一产出：

- ``IntentType``: 7 类细粒度意图枚举（值即路由名）。
- ``IntentStep``: 单步意图 = 意图类型 + 实体字典（query / pdf_path / paper_id / note_path /
  figure_ref / download / mode）。普通输入产生 1 个 step，复合意图由 Stage 3 拆分为有序 steps。
- ``IntentOutput``: 管线最终输出，Supervisor 直接消费。
  消费规则（ADR 0007）：steps=[...] → 逐 step spawn；steps=[] + clarification → ask_user 闭环；
  steps=[] + reply_suggestion → 直接回复。
- ``IntentionResult``: 管线统一出口 —— 封装 IntentOutput + 路由器原始决策
  （RouteChoice / 近失候选），供审计记录与 Stage 3 近失注入使用。
"""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from paperflow.core.intent.route import RouteChoice


class IntentType(str, Enum):
    """意图类型枚举，value 与路由名一致（routes.yaml 中的 name）。"""

    SEARCH_PAPER = "search_paper"
    DOWNLOAD_PAPER = "download_paper"
    READ_PAPER = "read_paper"
    ANSWER_QUESTION = "answer_question"
    QUERY_NOTES = "query_notes"
    GENERATE_NOTE = "generate_note"
    CHITCHAT = "chitchat"


class IntentStep(BaseModel):
    """单步意图：意图类型 + 实体字典。

    entities 键集合（ADR 0007）：query / pdf_path / paper_id / note_path /
    figure_ref / download / mode，由后续实体提取任务填充。
    """

    #: 意图类型
    intent: IntentType

    #: 实体字典（随意图类型不同而不同，无固定 schema）
    entities: dict[str, Any] = Field(default_factory=dict)


class IntentOutput(BaseModel):
    """意图管线最终输出契约（ADR 0007 输出契约一节）。

    steps 为空时走澄清/直接回复路径：clarification 非空 → Supervisor 调 ask_user；
    reply_suggestion 非空 → Supervisor 直接回复。两者互斥，由 Stage 3 保证。
    """

    #: 有序意图步骤列表；空列表表示无明确意图（走澄清或直接回复路径）
    steps: list[IntentStep] = Field(default_factory=list)

    #: 管线置信度（Stage 3 由 LLM 给出，Stage 2 来自 RouteChoice 相似度）
    confidence: float | None = None

    #: 产出来源（四级级联的哪一级）
    source: Literal["stage0", "stage1", "stage2", "stage3"]

    #: RouteChoice 的相似度分数（仅 Stage 2 有值），审计用
    similarity_score: float | None = None

    #: steps 为空时：ask_user 的问题（澄清闭环）
    clarification: str | None = None

    #: steps 为空时：Supervisor 直接回复的内容（chitchat）
    reply_suggestion: str | None = None

    #: Stage 3 发现的新意图写回候选（人工确认后入 routes.yaml）
    new_intent_candidate: dict[str, Any] | None = None

    #: 原始用户输入（审计用，逐字保留）
    raw_input: str


class IntentionResult(BaseModel):
    """意图管线统一出口。

    output 为结构化意图输出；route_choice / near_misses 保留路由器原始决策——
    前者记录命中的 RouteChoice（含相似度），后者是未达阈值但分数靠前的近失候选
    （Stage 3 LLM 兜底时注入提示词，辅助歧义消解与新意图发现）。
    """

    #: 结构化意图输出（Supervisor 实际消费的对象）
    output: IntentOutput

    #: Stage 2 命中的 RouteChoice（Stage 0/1/3 直出时为 None）
    route_choice: RouteChoice | None = None

    #: 近失候选（按相似度降序），供 Stage 3 注入
    near_misses: list[RouteChoice] = Field(default_factory=list)
