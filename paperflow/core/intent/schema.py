# paperflow/core/intent/schema.py
"""Route / RouteChoice —— 语义路由契约类型（对齐 semantic_router/schema.py 的精简版，静态路由）。

spec Section 5 要求用 dataclass 实现（非 pydantic BaseModel）：路由层只做
匹配判定，不需要 pydantic 的校验/序列化能力。相比 semantic-router 0.1.16 的
Route 去掉了动态路由字段（function_schemas / llm / metadata），因为意图路由只
使用 data/intents/routes.yaml 的静态配置。
"""

from dataclasses import dataclass, field


@dataclass
class Route:
    """一条意图路由：意图名 + 示例句集合 + per-route 阈值。

    ``score_threshold`` 覆盖路由器的全局阈值（semantic-router 的 per-route 阈值）；
    None 表示回落到 router.score_threshold。
    """

    #: 路由名，对应 IntentType 的 value（如 "search_paper"）
    name: str

    #: 该意图的示例句列表（中文，BM25 + 稠密编码共用）；default_factory 保证实例隔离
    utterances: list[str] = field(default_factory=list)

    #: per-route 阈值；None 表示使用路由器的全局 score_threshold
    score_threshold: float | None = None


@dataclass
class RouteChoice:
    """路由器对一次查询的判定结果。

    name=None + similarity_score=None 表示未命中任何路由（与 semantic-router 的
    ``RouteChoice()`` 空构造行为一致），供 pipeline 区分"无路由命中"走 Stage 3 LLM 兜底。
    """

    #: 命中的路由名；未命中时为 None
    name: str | None = None

    #: 命中的融合相似度分数（稠密/稀疏凸组合，clip 后非概率）；未命中时为 None
    similarity_score: float | None = None
