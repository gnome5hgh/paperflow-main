# paperflow/core/tokenization.py
"""Token 计数编码单点共享。

RAG 切块（``rag/parsers/chunker.py``）与 core.memory 上下文压缩
（``core/memory/compaction.py``）共用同一 tiktoken 编码口径：改 ``TOKEN_ENCODING``
会同时改变切块边界（需重建索引）与压缩 token 估算。放在 ``core`` 根下作叶子模块——
``rag`` 与 ``core.memory`` 都向下依赖它，不存在反向依赖。

tiktoken 只在 :func:`get_token_encoder` **函数内**惰性 import：模块级 import 会让
``import paperflow.config``（经 core.memory.compaction）被拖上 tiktoken，拖慢启动并
引入不必要依赖。
"""

#: tiktoken 编码器名。
#: - 值："cl100k_base"。
#: - 含义与单位：切块与上下文压缩共用的 token 计数编码器（GPT-4 系列）；近似计数即可。
#: - 改它的后果：token 计数口径变化 → 切块边界变化 → 必须重建索引；压缩估算阈值同步漂移。
#: - 是否进 YAML：否（L2 结构常量）。
TOKEN_ENCODING = "cl100k_base"

#: 进程级编码器单例（避免重复加载 BPE 文件）。
_encoder = None


def get_token_encoder():
    """返回进程级 tiktoken 编码器单例；首次调用才导入并加载 tiktoken。

    多 Agent 实例 / 逐消息估算 / 逐章节切块都会调用本函数，单例避免反复读
    BPE 文件。

    Returns:
        tiktoken.Encoding: 与 ``TOKEN_ENCODING`` 对应的编码器。
    """
    global _encoder
    if _encoder is None:
        import tiktoken
        _encoder = tiktoken.get_encoding(TOKEN_ENCODING)
    return _encoder
