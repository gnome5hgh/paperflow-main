# paperflow/core/intent/schemas/intent.py
"""意图判定的输出契约。

判定结果是**单个类别**（不是列表）：判定服务的选项表是单选，而「一轮里提了几件
事」这种信息本来就在用户原句里，supervisor 自己读得出来——为了把它变成结构化字段
而引入一条阈值（多标签需要）不划算。
"""
from pydantic import BaseModel, Field

from paperflow.core.intent.constants import IntentStep, IntentType

__all__ = ["IntentOutput"]


class IntentOutput(BaseModel):
    """一轮判定的产出：类别 + 把握 + 实体 + 来源。

    Attributes:
        intent: IntentType，本轮类别（11 值之一）。
        confidence: float | None，判定把握。规则命中是确定性的，记 1.0；判定服务给的
            是它选中项的概率。**没有判定消费者**，只作展示与排查线索。
        entities: dict[str, str]，确定性抽取的实体（pdf_path / arxiv_id / doi /
            note_path / figure），供 supervisor 直接拼进子任务文本。
        source: IntentStep，本轮判定来自规则层还是判定服务。
    """

    intent: IntentType
    confidence: float | None = None
    entities: dict[str, str] = Field(default_factory=dict)
    source: IntentStep
