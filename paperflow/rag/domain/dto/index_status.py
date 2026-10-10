"""索引体检快照 dto：只读，不触发任何写入。"""
from dataclasses import dataclass, field


@dataclass
class IndexStatus:
    """索引体检快照（只读，不触发任何写入）。

    Attributes:
        state_present: bool，状态文件是否存在且可解析
        state_version: object，状态文件里记的配方哈希（None = 无状态）
        recipe: str，当前配置算出的配方哈希
        recipe_in_sync: bool，状态版本是否与当前配方一致（不一致 → 下次收敛会全量重扫）
        indexed_docs: int，状态文件记录的文档数
        store_chunks: int，向量库中的块数
        store_docs: int，向量库涉及的文档数（去重）
        bm25_docs: int，内存关键词索引里的文档数
        ghost: list[str]，状态有记录但磁盘已不存在的文档（相对路径）——删除未收敛的残留
        not_indexed: list[str]，语料根下有文件但状态里没有（相对路径）——新增未入库
        corpus_docs: int，语料根下扫描到的 PDF 篇数
        milvus_ok: bool，Milvus 可连性（探测带 TTL）
    """

    state_present: bool = False
    state_version: object = None
    recipe: str = ""
    recipe_in_sync: bool = False
    indexed_docs: int = 0
    store_chunks: int = 0
    store_docs: int = 0
    bm25_docs: int = 0
    ghost: list = field(default_factory=list)
    not_indexed: list = field(default_factory=list)
    corpus_docs: int = 0
    milvus_ok: bool = False
