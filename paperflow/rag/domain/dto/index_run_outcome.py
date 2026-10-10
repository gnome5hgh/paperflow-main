"""全量扫描结果统计 dto。"""
from dataclasses import dataclass


@dataclass
class IndexRunOutcome:
    """全量扫描的结果统计。

    Attributes:
        changed: int，本次重新索引的文档数
        removed: int，本次清理的已删除文档数
        chunks: int，本次写入的块数合计
        bm25_docs: int，重建后 BM25 中的文档数
        recipe_reset: bool，是否因配方哈希不符（或状态缺失）放弃旧状态走了全量重扫
        images_swept: int，本次清掉的孤儿图表原图数（库里已无对应块的对象）
    """
    changed: int = 0
    removed: int = 0
    chunks: int = 0
    bm25_docs: int = 0
    recipe_reset: bool = False
    images_swept: int = 0
