"""检索器：融合 BM25 关键词与向量检索的候选，用 RRF 算法合并排序，再交给重排模型精排。

索引为空时返回空结果。对外检索工具 RagRetrieveTool 在 `paperflow/tools/rag/rag_retrieve.py`。
"""
from paperflow.rag.parsers.chunker import Chunk


# ---- 混合检索参数配置 ----
# 两路各取 30 个粗候选，保证召回率
# RRF 融合常数 k=60，值越大排名间的分数差异越平滑
_RRF_K = 60
_BM25_TOPK = 30
_VECTOR_TOPK = 30

#: query 侧任务指令（Qwen3-Embedding 官方格式 Instruct: {task}\nQuery: {query}，
#: 只加 query 侧、文档侧不加，官方称可提升 1–5%）。
# 文档编码在索引器完成，不受影响；意图路由复用同一 embedder，也走各自调用、无此前缀。
_QUERY_INSTRUCTION = ("Given an academic research query, retrieve relevant "
                      "passages from papers and reading notes")

#: source 过滤的合法取值；超出按不过滤处理（工具层已有 enum 约束，此处防御）
_VALID_SOURCES = (None, "note", "pdf")


class Retriever:
    """融合检索引擎：同时跑 BM25 关键词检索与向量检索，RRF 合并，再精排。

    与检索工具 RagRetrieveTool（tools/rag）是两类职责：这里实现检索与融合算法；
    RagRetrieveTool 只做对外暴露的薄封装（取单例、持锁、格式化结果）。
    """

    def __init__(self, service):
        """绑定门面服务：底层组件（向量库/BM25/编码器/重排器）都经 service 惰性获取。"""
        self.service = service
        # BM25 进程级同步标记：BM25 是内存投影，进程重启即空，首次查询前必须
        # 从向量库整体重建一次才能与向量路对齐；此后增删改由索引器与向量库
        # 成对执行维护，无需再重建。
        self._bm25_synced = False

    def retrieve(self, query: str, top_k: int = 5,
                 source: str | None = None) -> list[Chunk]:
        """对 query 执行检索，返回按相关度排序的前 top_k 个块。

        Args:
            source: 限定来源——"note" 只搜笔记，"pdf" 只搜论文；None 不过滤。
                    非法值按 None 处理（防御性）。

        并发约定：调用方（RAGService.retrieve 或 RagRetrieveTool）已持有锁，
        此处不再加锁——锁由工具层/门面层统一控制，若在此重复加锁会造成
        死锁或锁语义混乱（防止后人「顺手补锁」的关键 WHY）。

        边界条件：
        - BM25 索引为空 → 只用向量检索；两路均为空 → 返回空列表。
        - 重排返回的下标越界 → 安全截断（防御性）。
        """
        if source not in _VALID_SOURCES:
            source = None

        embedder = self.service._ensure_embedder()
        # 仅 query 侧加指令前缀（Qwen3 官方格式）
        qvec = embedder([f"Instruct: {_QUERY_INSTRUCTION}\nQuery: {query}"])[0]

        vs = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # ---- BM25 进程级恢复（裸 rebuild）----
        # 每进程同步一次，而非「空了才补」：重启后若先发生一次写热更新，
        # BM25 只含新写的一篇，与向量库已漂移（非空但残缺），is_empty 探测不到。
        # 重建以向量库原文为唯一源，只恢复 BM25 内存索引，
        # 不触碰索引状态与文档块（那是 index_all 全量重扫的职责，个人语料规模下两者开销差一个量级）。
        # 重建失败（Milvus 读取异常）不置位，下次查询重试；本次退化为纯向量路。
        if not self._bm25_synced:
            bm25.rebuild([(d[0], d[1]) for d in vs.all_documents()])
            self._bm25_synced = True

        # ---- 双路检索（向量路结果自带 text/path/source，不再全表回查）----
        expr = f'source == "{source}"' if source else ""
        vec_hits = vs.query(qvec, _VECTOR_TOPK, expr=expr)   # (id, text, path, source, dist)
        bm25_hits = bm25.query(query, _BM25_TOPK) if not bm25.is_empty() else [] # list[str]，元素是chunk id，长度 ≤ _BM25_TOPK
        bm25_docs = {d[0]: d for d in vs.fetch_by_ids(bm25_hits)}   # id -> (id, text, path, source)
        if source:
            # BM25 路无原生过滤，取回元数据后按 source 筛（候选变少可接受，个人语料规模下不为此放大 top）
            bm25_hits = [i for i in bm25_hits
                         if i in bm25_docs and bm25_docs[i][3] == source]

        # ---- RRF 融合两路结果 ----
        # 累计每个块的总分：dict 键是块 id，值是两路贡献的 RRF 分数之和
        scores: dict[str, float] = {}

        # 第一遍：BM25 路的名单（list[str]，按 BM25 分数降序，只有 id）
        # enumerate 给出名次：rank=0 是第 1 名，rank=1 是第 2 名……
        for rank, doc_id in enumerate(bm25_hits):
            # 该块每出现一路，就累加一份「倒数排名」分：
            # 第 1 名得 1/(60+0)≈0.0167，第 2 名得 1/(60+1)≈0.0164……
            # scores.get(doc_id, 0.0)：这个块此前没出现过就当 0 分起算（第一次加分），
            # 出现过（比如 BM25 里第 3、向量路里也第 5）就在旧分上累加
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)

        # 第二遍：向量路的名单（五元组，第 0 位才是 id，所以写 hit[0]）
        # 与上面完全同一套算分——两路的"名次"被换算到同一把尺子上后才能相加
        for rank, hit in enumerate(vec_hits):
            scores[hit[0]] = scores.get(hit[0], 0.0) + 1.0 / (_RRF_K + rank)

        # 两路名单合起来一个块都没有（索引空/查询无命中）→ 直接返回空
        if not scores:
            return []

        # ---- 候选池与精排 ----
        # 精排前的池子大小。取 max(2×top_k, 配置值)：
        #   top_k * 2      —— 至少是最终要的数量的两倍，给精排留挑选余地；
        #   配置值（默认24）—— 下限兜底：top_k 很小（比如 3）时，2×3=6 的池子太小。
        # 为什么要"宽"：RRF 是按名次累加的，一个真命中如果只在其中一路排第一、
        # 另一路没进 top30，它的总分只算一路，会被两路都中等的平庸块压到中游。
        # 池子切小了，这些块在进精排（真正懂语义的评委）之前就被淘汰了。
        # BAAI 惯例：宽召回窄输出——粗排负责不漏，精排负责排准。
        candidates = max(top_k * 2,
                         int(getattr(self.service.config, "rag_rerank_candidates", 24)))

        # 按 RRF 总分从高到低排序，取前 candidates 个 id。
        # sorted 的对象是 scores 的键（块 id），key=scores.get 是"取该键的分数来比"——
        # .get 不带括号是传方法本身，sorted 会对每个 id 调一次它；
        # reverse=True 降序；[:candidates] 截断成候选池
        ranked_ids = sorted(scores, key=scores.get, reverse=True)[:candidates]

        # id -> (id, text, path, source) 的统一档案表。
        # 两路拿档案的方式不同：向量路查询时元数据随结果带回（五元组，hit[0] 是 id），
        # BM25 路只有 id，之前已用 fetch_by_ids 回库补了档案（bm25_docs）。
        # 这里把两份档案并成一个 dict；
        # update 后写的 bm25_docs 覆盖同 id 条目——两路的 text/path/source 本就同源，覆盖无损
        id2doc: dict[str, tuple] = {hit[0]: hit for hit in vec_hits}
        id2doc.update(bm25_docs)

        # 防御性求交集：只保留"总分进了候选池、且档案真实存在"的 id。
        # 正常情况下 ranked_ids ⊆ id2doc（scores 的 id 就来自这两份档案），
        # 这行是兜底——万一某 id 在库里已被删/取档案失败，静默剔除而不是炸 KeyError
        present = [i for i in ranked_ids if i in id2doc]
        # 精排模型要看的文本：按候选池顺序抽出每条的原文
        docs = [id2doc[i][1] for i in present]

        # 重新组装成 Chunk。heading 恒空、chunk_index 恒 0：
        # 检索端不做前缀行解析（块的「标题 > 章节」前缀行与正文首行之间没有可靠的区分标记），
        # 标题上下文就让块文本首行自然携带、随摘录一起展示
        chunks = [Chunk(id=i, text=id2doc[i][1], path=id2doc[i][2],
                        source=id2doc[i][3], heading="", chunk_index=0)
                  for i in present]

        # 精排：cross-encoder 模型把 (query, 每条候选文本) 成对送进模型打分，
        # 返回"按相关度降序的下标列表"，只取前 top_k 个下标
        reranker = self.service._ensure_reranker()
        # 比如：[3, 1, 2, 0, 4] 表示第1名是 doc3，第2名是 doc1，……
        order = reranker(query, docs, top_k)
        # 下标 → Chunk 对象。i < len(chunks) 是防御：万一精排返回越界下标，
        # 安全截断而不是 IndexError（docstring 里声明的边界条件之一）
        return [chunks[i] for i in order if i < len(chunks)]
