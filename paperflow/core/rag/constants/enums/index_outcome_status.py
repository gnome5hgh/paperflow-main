"""单篇文档入库的结果状态。

产出在索引器（一次 index_document 落到哪个结局），判定在工具层（按它统计
「成功几篇 / 未入库几篇」并决定给用户报什么）。两处必须对得上，故词表单列。
"""

from enum import StrEnum

__all__ = ["IndexOutcomeStatus"]


class IndexOutcomeStatus(StrEnum):
    """一次入库尝试的结局。"""

    #: 已入库，chunks 为本次写入的块数
    INDEXED = "indexed"
    #: 未入库：文件不存在、不在语料根下（只索引 PDF）
    SKIPPED = "skipped"
    #: 旧块已清理但新内容切不出块——索引里不再有这篇，属于「删干净了但没建起来」
    EMPTY = "empty"
