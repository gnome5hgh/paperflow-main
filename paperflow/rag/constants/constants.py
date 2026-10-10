"""RAG 域的跨文件共享常量：块 id 与块类型。

只收**跨文件共享、且不属于某一个消费方**的常量。块 id 前缀长度与块类型取值同时被
切块方（`parsers/chunker.py`）与索引方（`services/indexer.py`）读写——切块时生成、
入库时按同一规则重建与筛选——改一边就会让两边对不上，所以它们有唯一声明点。
正则、提示词、退避基数、容量阈值这类只服务单一消费方的结构常量留在消费处就地声明。
"""

__all__ = ["CHUNK_ID_LEN", "CHUNK_TYPE_FIGURE", "CHUNK_TYPE_TABLE", "CHUNK_TYPE_TEXT"]

#: 块 id 的哈希前缀长度（字符）：块 id 取 ``sha1(绝对路径:序号)`` 十六进制串的
#: 前 N 个字符。结构契约——改它所有块 id 变化，必须全量重建索引，否则旧块残留、
#: 新块 id 对不上（indexer 的「先删后建」依赖 id 稳定，与 chunker 共用同一规则）。
CHUNK_ID_LEN = 16

#: 块类型取值：普通正文块 / 表格块 / 插图块。媒体块（表、图）的正文是区域内
#: 文字、字幕字段另存注文原文，两者在检索结果里分开呈现。
CHUNK_TYPE_TEXT = "text"
CHUNK_TYPE_TABLE = "table"
CHUNK_TYPE_FIGURE = "figure"
