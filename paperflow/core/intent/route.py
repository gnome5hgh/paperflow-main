# paperflow/core/intent/route.py
"""
Route / RouteChoice —— 语义路由契约类型（复现 semantic-router 0.1.16 的静态路由子集）。

- ``Route``: 意图知识库中的一条路由 = 意图名 + 若干示例句（utterances）+ 可选阈值/元数据。
  ADR 0007 的意图路由只使用静态路由（配置在 data/intents/routes.yaml），
  因此相比 semantic-router 0.1.16 的 Route 去掉了动态路由字段（function_schemas / llm）。
- ``RouteChoice``: 路由器对一次查询的判定结果。未命中任何路由时返回空实例
  （name=None，与 semantic-router 的 ``RouteChoice()`` 空构造行为一致），
  命中时 similarity_score 携带混合分数供 Stage 3 近失注入使用。
"""

from typing import Any

from pydantic import BaseModel, Field


class RouteChoice(BaseModel):
    """
    路由判定结果，语义等价于 semantic_router.schema.RouteChoice。

    name=None 表示未命中任何路由（空 RouteChoice）。function_call 为动态路由
    （函数 schema 参数抽取）产物，静态路由下恒为 None，保留字段以对齐上游契约。
    """

    #: 命中的路由名；未命中时为 None
    name: str | None = None

    #: 动态路由抽取出的函数调用参数；静态路由下恒为 None
    function_call: list[dict[str, Any]] | None = None

    #: 命中的相似度分数（稠密/稀疏凸组合），未命中时为 None
    similarity_score: float | None = None


class Route(BaseModel):
    """
    一条意图路由（静态路由）。

    ``utterances`` 为该意图的示例句集合（中文，BM25 + 稠密编码共用），
    ``score_threshold`` 覆盖路由器的全局阈值（semantic-router 的 per-route 阈值），
    ``metadata`` 携带 Supervisor 调度所需的映射信息（如 subagent 名、download 开关）。
    """

    #: 路由名，对应 IntentType 的 value（如 "search_paper"）
    name: str

    #: 该意图的示例句列表（≥1 条，用于编码与相似度匹配）
    utterances: list[str]

    #: 路由描述（供日志/审计阅读，不参与匹配）
    description: str | None = None

    #: per-route 阈值；None 表示使用路由器的全局 score_threshold
    score_threshold: float | None = None

    #: 调度映射元数据（subagent / download / mode 等）
    metadata: dict[str, Any] = Field(default_factory=dict)
