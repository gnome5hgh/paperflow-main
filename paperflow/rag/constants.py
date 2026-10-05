# paperflow/rag/constants.py
"""RAG 模块 L2 常量 registry——需重标定/重索引/改变评测口径的算法常量单一真相源。

分层判据（spec 2026-10-05 §3）：改了它需要重跑评测标定/重建索引/缓存 → 代码层
本文件；改了立即生效、对数据一致性安全 → config.yaml。本文件只收「不进 YAML」的
L2 常量。core/llm 的传输常量（batch/timeout/retries/backoff）放
``paperflow/core/llm/constants.py``——core 是下层，不能反向依赖 rag，否则违反分层。

每条常量固定四要素：值 / 含义与单位 / 改它的后果（要重标定？影响评测口径？）/
是否进 YAML。PR B 会把部分常量接成 ``rag.*`` YAML 字段的默认值来源；YAML 键本身
不经本文件写入（本文件不 import config）。

不搬的 L3 结构契约：正则/词表（``_REFERENCE_HEADS``、``_SENT_SPLIT_RE``、``_TEI_NS``）、
prompt 文本（``_QUERY_INSTRUCTION``）、Milvus schema 长度 / HNSW 参数（需重建集合，
不在本 PR 范围）。原 ``indexer._STATE_VERSION`` 已在 Task 7 由配方哈希
（``indexer._recipe_hash``，输入含本文件的 ``RECIPE_LOGIC_REVISION``）取代。
"""

# ── 混合检索（services/retriever.py 消费） ───────────────────────────────────

#: RRF 倒数排名融合常数 k。
#: - 值：60。
#: - 含义与单位：每条查询每路命中的分数贡献为 ``1 / (RRF_K + rank)``（rank 从 0 起）。
#:   无量纲正数；越大则相邻名次间的分数差越小、融合越平滑。
#: - 改它的后果：改变融合排序与最终重排候选集，需重跑检索质量评测；不触发重建索引。
#: - 是否进 YAML：否；PR B 将作为 ``rag.retriever.rrf_k`` 的默认值来源。
RRF_K = 60

#: BM25 稀疏路每查询的粗召回数。
#: - 值：30。
#: - 含义与单位：每条 query 从 BM25 内存索引取回的候选块数（条）。
#: - 改它的后果：改变参与 RRF 融合的候选面，影响召回率与检索耗时，需重评检索质量；
#:   不触发重建索引。
#: - 是否进 YAML：否；PR B 将作为 ``rag.retriever.bm25_topk`` 的默认值来源。
BM25_TOPK = 30

#: 向量路每查询的粗召回数。
#: - 值：30。
#: - 含义与单位：每条 query 从向量库取回的候选块数（条）。
#: - 改它的后果：改变参与 RRF 融合的候选面，影响召回率与检索耗时，需重评检索质量；
#:   不触发重建索引。
#: - 是否进 YAML：否；PR B 将作为 ``rag.retriever.vector_topk`` 的默认值来源。
VECTOR_TOPK = 30

#: 重排候选池下限。
#: - 值：24。
#: - 含义与单位：RRF 融合后交给重排模型的候选块数下限（条）；实际池大小为
#:   ``max(top_k * RERANK_CANDIDATE_MULTIPLIER, RERANK_CANDIDATES)``。宽召回窄输出
#:   （BAAI 官方教程召回 100 → 精排 3）。
#: - 改它的后果：池子越大重排越慢越费 token、真命中截损越小；改变精排输入，需重评。
#:   不触发重建索引。
#: - 是否进 YAML：否；PR B 将作为 ``rag.retriever.rerank_candidates`` 的默认值来源。
RERANK_CANDIDATES = 24

#: 重排候选池随 top_k 放大的倍率。
#: - 值：2。
#: - 含义与单位：候选池下限公式 ``top_k * 本值`` 中的倍率（无量纲）；保证大 top_k
#:   时池子同步放大，不至于只比 top_k 略大。
#: - 改它的后果：改变大 top_k 场景下的精排候选面与开销，需重评；不触发重建索引。
#: - 是否进 YAML：否（与 RERANK_CANDIDATES 同属精排池口径）。
RERANK_CANDIDATE_MULTIPLIER = 2

#: 检索默认返回块数。
#: - 值：5。
#: - 含义与单位：``Retriever.retrieve`` 与工具 ``execute`` 未显式传 top_k 时的返回
#:   块数（条）。
#: - 改它的后果：改变所有默认调用的返回条数与上下文长度；工具 schema 不再
#:   advertise 该值（模型省略 top_k 即落到 rag.retriever.top_k 配置）。
#: - 是否进 YAML：否；PR B 将作为 ``rag.retriever.top_k`` 的默认值来源。
DEFAULT_TOP_K = 5

# ── 分块（parsers/chunker.py、services/indexer.py 消费） ─────────────────────

#: 每个检索块的最大 token 数。
#: - 值：512。
#: - 含义与单位：AcademicChunker 二次切分的块预算（token，按
#:   ``core.tokenization.TOKEN_ENCODING`` 近似计数）。
#:   嵌入模型支持 32K 上下文，512 是检索粒度的选择：太大召回噪声多、太小语义碎片化。
#: - 改它的后果：改变切块结果 → 必须重建索引（PR B 的配方哈希会保护）；影响检索粒度，
#:   需重评召回质量。
#: - 是否进 YAML：否；PR B 将作为 ``rag.chunker.max_tokens`` 的默认值来源（配方哈希保护）。
CHUNK_MAX_TOKENS = 512

#: 相邻检索块的重叠 token 数。
#: - 值：64。
#: - 含义与单位：新窗从上一窗尾部回收的完整句 token 预算（token），约为块长的 1/8；
#:   让跨块语义连贯。
#: - 改它的后果：改变切块结果 → 必须重建索引（配方哈希保护）；影响跨块召回。
#: - 是否进 YAML：否；PR B 将作为 ``rag.chunker.overlap_tokens`` 的默认值来源。
CHUNK_OVERLAP_TOKENS = 64

#: 块 id 的哈希前缀长度。
#: - 值：16。
#: - 含义与单位：块 id 取 ``sha1(相对路径:序号)`` 十六进制串的前 N 个字符（字符）。
#:   chunker 的章节块与 indexer 的表格/图注块共用同一规则。
#: - 改它的后果：所有块 id 变化 → 必须全量重建索引，否则旧块残留、新块 id 对不上
#:   （「先删后建」依赖 id 稳定）。
#: - 是否进 YAML：否（属结构契约量级，改动等价于一次全量重建）。
CHUNK_ID_LEN = 16

#: 表格块文本截断上限。
#: - 值：8000。
#: - 含义与单位：indexer 写入表格块前截断的字符数（字符）；Milvus text 字段上限 65535
#:   的防御性截断。
#: - 改它的后果：改变表格块内容长度 → 必须重建索引；过大可能逼近 Milvus 字段上限。
#: - 是否进 YAML：否；PR B 将作为 ``rag.indexer.table_text_limit`` 的默认值来源。
TABLE_TEXT_LIMIT = 8000

#: 配方哈希的逻辑版本号（切块/解析「逻辑」修订号，非参数）。
#: - 值：1。
#: - 含义与单位：``indexer._recipe_hash`` 的输入之一。参数（chunker 的 max/overlap、
#:   table_text_limit、embed_model）自动进指纹；切块/解析的**算法逻辑**（如
#:   ``_pack_sentences`` 改写、章节/媒体块产出规则变更）无法被参数枚举，只能手动 +1。
#: - 改它的后果：配方哈希变 → 下次 ``index_all`` 放弃旧状态、全量重扫重嵌（预期的一次性
#:   重索引）。仅当切块/解析逻辑改动、产出块集合可能变化时才 +1，不要为参数调整而改。
#: - 是否进 YAML：否（L2 结构常量；属失效机制本身的版本号）。
RECIPE_LOGIC_REVISION = 1

# ── 查询改写（services/query_rewriter.py 消费） ──────────────────────────────

#: 改写要求生成的 rewrites 条数。
#: - 值：3。
#: - 含义与单位：发给 LLM 的 prompt 中要求的改写变体条数（条）；生成侧实际席位由
#:   MAX_QUERIES-1 截断，本值定义「要求几条」的口径。
#: - 改它的后果：改变改写 prompt 与查询集规模，影响召回率与检索延迟，需重评。
#: - 是否进 YAML：否。
REWRITE_NUM = 3

#: 最终查询集封顶。
#: - 值：4。
#: - 含义与单位：改写后查询集的最大条数（条，含原 query）；生成侧最多占
#:   ``MAX_QUERIES - 1`` 席，最后 1 席恒定留给原 query 作召回兜底。
#: - 改它的后果：改变查询集规模与检索开销（每 query 一趟双路检索），需重评。
#: - 是否进 YAML：否。
MAX_QUERIES = 4

#: 单条查询字符上限。
#: - 值：200。
#: - 含义与单位：改写输出逐项过滤时，超过此长度视为 LLM 输出异常并丢弃（字符）；
#:   Haystack 式逐项过滤。
#: - 改它的后果：改变合法查询的接收边界（可能误丢长查询或误收异常输出）。
#: - 是否进 YAML：否。
MAX_QUERY_CHARS = 200

#: 改写结构化输出的解析失败重试次数。
#: - 值：0。
#: - 含义与单位：StructuredOutput 解析失败后的重试次数（次）；0 = 不重试，失败即降级
#:   为 [原 query]，单次检索的失败延迟上限 = 1 次 LLM 调用（spec §6 与主流对齐）。
#: - 改它的后果：改变失败路径的额外 LLM 调用次数与检索延迟上限。
#: - 是否进 YAML：否。
REWRITE_MAX_RETRIES = 0

#: 单条历史消息截断长度。
#: - 值：300。
#: - 含义与单位：拼进改写 prompt 时每条 user/assistant 消息保留的字符数（字符）。
#: - 改它的后果：改变改写 prompt 体量与指代消解质量，影响改写结果。
#: - 是否进 YAML：否。
HISTORY_MESSAGE_CHARS = 300

#: 最近对话历史条数。
#: - 值：6。
#: - 含义与单位：喂给查询改写（condense）的最近消息条数（条，user/assistant 各算一条）。
#:   原 ``_HISTORY_MESSAGES`` 与 ``_HISTORY_LIMIT`` 的合并；Task 7 起运行期经
#:   ``rag.query_rewrite.history_messages`` 配置传入 ``QueryRewriter`` 与
#:   ``tools/rag/rag_retrieve._recent_history``（本值退居该 YAML 字段的默认值来源）。
#: - 改它的后果：改变改写输入上下文长度与检索延迟，影响指代消解质量。
#: - 是否进 YAML：否；PR B 将作为 ``rag.query_rewrite.history_messages`` 的默认值来源。
HISTORY_MESSAGES = 6

# ── 检索工具（tools/rag/rag_retrieve.py 消费） ──────────────────────────────

#: source 过滤的合法取值。
#: - 值：(None, "note", "pdf")。
#: - 含义与单位：Retriever 接受的 source 过滤集合（元组）；超出按不过滤处理（工具层
#:   已有 enum 约束，此处防御）。
#: - 改它的后果：改变过滤取值口径（如新增来源类型）；不触发重建索引。
#: - 是否进 YAML：否（L3 结构契约）。
VALID_SOURCES = (None, "note", "pdf")

#: 工具输出单条命中的正文摘录上限。
#: - 值：400。
#: - 含义与单位：RagRetrieveTool 每条命中截取的正文字符数（字符）。带「标题 > 章节」
#:   前缀的块，摘录首行即该前缀，需要足够窗口让上层同时拿到节号与正文。
#: - 改它的后果：改变工具返回体量与上层可用上下文窗口；不触发重建索引。
#: - 是否进 YAML：否；PR B 将作为 ``rag.tools.excerpt_chars`` 的默认值来源。
EXCERPT_CHARS = 400

# ── 存储 / 解析（storage/vector_store.py、parsers/grobid_client.py 消费） ─────

#: Milvus all_documents 分页遍历的每页行数。
#: - 值：1000。
#: - 含义与单位：query_iterator 每次取回的块行数（行），规避单次 query 16384 行上限；
#:   测试可传小值验证跨页。
#: - 改它的后果：仅影响全表读取的内存峰值与往返次数，不改变数据；不触发重建索引、无需重标定。
#: - 是否进 YAML：否；作为 ``rag.storage.batch_size`` 的默认值来源。
MILVUS_BATCH_SIZE = 1000

#: GROBID HTTP 请求超时。
#: - 值：60.0。
#: - 含义与单位：GrobidClient 的 httpx 客户端超时（秒），覆盖健康检查与全文解析请求。
#: - 改它的后果：改变 GROBID 慢响应容忍度与解析失败降级时点（超时后退 PyMuPDF）；
#:   影响解析结果来源进而影响切块，但不触发已索引文档的重扫（配方哈希默认不纳入 GROBID）。
#: - 是否进 YAML：否；作为 ``rag.grobid.timeout`` 的默认值来源。
GROBID_TIMEOUT = 60.0
