# paperflow/core/llm/constants.py
"""core/llm 传输层 L2 常量 registry——嵌入/精排客户端的批大小、超时、重试与退避。

分层判据（spec 2026-10-05 §3）：改了它需要重跑评测标定/重建索引/缓存 → 代码层
本文件；改了立即生效安全 → config.yaml。本文件只收「不进 YAML」的 L2 常量。

为什么放 core/llm 而非 rag/constants.py：core 是下层，rag/cli 依赖它；若把这些
常量放进 rag，core/llm 的 embedding.py/rerank.py 就会反向 import rag，违反 spec 的
分层依赖方向。

每条常量固定四要素：值 / 含义与单位 / 改它的后果 / 是否进 YAML。
"""

# ── 嵌入（embedding.py 消费） ────────────────────────────────────────────────

#: 单批嵌入请求的文本条数。
#: - 值：32。
#: - 含义与单位：CloudEmbedder 每次 ``/v1/embeddings`` 请求送入的文本条数（条）；
#:   大批量按此切片顺序请求。
#: - 改它的后果：改变请求数、吞吐与内存峰值；不改变向量结果、无需重建索引或重标定。
#: - 是否进 YAML：否；PR B 将作为 ``rag.embedding.batch_size`` 的默认值来源。
EMBED_BATCH_SIZE = 32

#: 嵌入 HTTP 客户端读超时。
#: - 值：60.0。
#: - 含义与单位：httpx.Client 的读超时（秒）。
#: - 改它的后果：改变慢响应的容忍度与最坏等待时长；不影响向量结果。
#: - 是否进 YAML：否；PR B 将作为 ``rag.embedding.timeout`` 的默认值来源。
EMBED_TIMEOUT = 60.0

#: 嵌入可恢复错误的重试次数。
#: - 值：2。
#: - 含义与单位：连接错误/超时/5xx（及 408/429）的重试次数（次）；4xx 与客户端构造
#:   错误立即失败不重试。
#: - 改它的后果：改变网络抖动下的重试次数与最坏延迟；冷启动预算敏感（spec §1）。
#: - 是否进 YAML：否；PR B 将作为 ``rag.embedding.max_retries`` 的默认值来源。
EMBED_MAX_RETRIES = 2

# ── 精排（rerank.py 消费） ───────────────────────────────────────────────────

#: 精排 HTTP 客户端读超时。
#: - 值：60.0。
#: - 含义与单位：httpx.Client 的读超时（秒）。
#: - 改它的后果：改变慢响应的容忍度与最坏等待时长；不影响排序结果。
#: - 是否进 YAML：否。
RERANK_TIMEOUT = 60.0

#: 精排可恢复错误的重试次数。
#: - 值：2。
#: - 含义与单位：连接错误/超时/5xx（及 408/429）的重试次数（次）；与嵌入侧语义对齐。
#: - 改它的后果：改变网络抖动下的重试次数与最坏延迟。
#: - 是否进 YAML：否。
RERANK_MAX_RETRIES = 2

# ── 重试退避（embedding.py / rerank.py 共用） ────────────────────────────────

#: 指数退避基数。
#: - 值：0.5。
#: - 含义与单位：第 attempt 次重试前 ``sleep(RETRY_BACKOFF_BASE * 2 ** attempt)`` 秒
#:   （base 单位为秒）；attempt 从 0 起。
#: - 改它的后果：改变重试等待时长与最坏延迟；不影响结果。
#: - 是否进 YAML：否。
RETRY_BACKOFF_BASE = 0.5
