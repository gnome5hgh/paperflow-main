# paperflow/core/intent/schemas/route.py
"""Route / RouteChoice —— 意图路由的路由契约类型（静态路由版）。

这是"路由契约"：定义路由器的输入（Route）与输出（RouteChoice），
与 schemas/intent.py 的"输出契约"（意图识别结果）职责不同。

用 dataclass 而非 pydantic BaseModel 实现：路由层只做匹配判定，
不需要 pydantic 的校验/序列化能力。字段只保留静态路由所需的
「意图名 + 示例句集合 + 专属阈值」——意图路由完全由
data/intents/routes.yaml 的静态配置驱动，不需要动态函数 schema /
LLM / 元数据等运行时字段。
"""

from dataclasses import dataclass, field


@dataclass
class Route:
    """一条意图路由：意图名 + 示例句集合 + 该路由专属阈值。

    ``score_threshold`` 覆盖路由器的全局阈值；为 None 时使用路由器的全局阈值。
    """

    #: 路由名，对应 IntentType 的枚举值（如 "search_paper"）
    name: str

    #: 该意图的示例句列表（中文，稀疏与稠密编码共用）；default_factory 保证实例隔离
    utterances: list[str] = field(default_factory=list)

    #: 该路由专属阈值；None 表示使用路由器的全局 score_threshold
    score_threshold: float | None = None


@dataclass
class RouteChoice:
    """路由器对一次查询的判定结果。

    name=None + similarity_score=None 表示未命中任何路由，供管线区分
    "无路由命中"而走 LLM 兜底分支。
    """

    #: 命中的路由名；未命中时为 None
    name: str | None = None

    #: 命中的融合相似度分数（稠密/稀疏凸组合，clip 后非概率）；未命中时为 None
    similarity_score: float | None = None
