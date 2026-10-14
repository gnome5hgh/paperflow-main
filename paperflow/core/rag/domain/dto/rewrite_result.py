"""查询改写结果 dto。"""
from dataclasses import dataclass


@dataclass
class RewriteResult:
    """改写结果：queries 是最终查询集（queries[0] 即主查询，供 reranker 打分）。

    degraded=True 表示 LLM 失败已降级——queries 退化为 [原query]。

    Attributes:
        queries: list[str]，最终查询集（queries[0] 即主查询，供 reranker 打分）
        standalone: str，消解指代后的自包含查询
        degraded: bool，True 表示 LLM 失败已降级（queries 退化为 [原query]）
    """

    queries: list[str]
    standalone: str
    degraded: bool = False
