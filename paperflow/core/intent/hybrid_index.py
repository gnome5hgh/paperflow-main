"""对齐 semantic_router/index/hybrid_local.py 的 HybridLocalIndex。"""
import numpy as np
from numpy.linalg import norm


class HybridLocalIndex:
    """稠密 + 稀疏双索引（内存版）。

    融合查询 = 稠密余弦相似度（sim_d）+ 稀疏点积（sim_s），
    用 argpartition 取 top_k（不排序全量，O(n) 复杂度取前 k）。
    稀疏索引存 {token_id: weight} 字典列表，点积时逐 doc 求和——
    稀疏向量维度远大于稠密 dim，字典表示避免零值浪费。"""

    def __init__(self):
        self.index: np.ndarray | None = None        # (n, dim) 稠密（已 alpha 缩放）
        self.sparse_index: list[dict] | None = None  # [{token_id: weight}]（已 1-alpha 缩放）
        self.routes: np.ndarray | None = None
        self.utterances: np.ndarray | None = None

    def add(self, embeddings, routes, utterances, sparse_embeddings) -> None:
        """对齐 add()：首次初始化或 concat 追加。

        首次 add 直接赋值；后续 add 用 np.concatenate 追加（保持 index 是
        单一 ndarray，query 里 norm/dot 向量化一次性算完，不逐行循环）。"""
        embeds = np.array(embeddings)
        routes_arr = np.array(routes)
        utts_arr = np.array(utterances)
        if self.index is None:
            self.index = embeds
            self.sparse_index = [dict(x) for x in sparse_embeddings]
            self.routes = routes_arr
            self.utterances = utts_arr
        else:
            self.index = np.concatenate([self.index, embeds])
            self.sparse_index.extend(dict(x) for x in sparse_embeddings)
            self.routes = np.concatenate([self.routes, routes_arr])
            self.utterances = np.concatenate([self.utterances, utts_arr])

    def query(self, vector, top_k: int = 5,
              sparse_vector: dict[int, float] | None = None
              ) -> tuple[np.ndarray, list[str]]:
        """对齐 query()：sim_d（余弦）+ sim_s（稀疏点积）→ argpartition top_k。

        sim_d：余弦相似度，除以各行范数 * query 范数（防止 query 未归一化）。
        sim_s：稀疏点积（BM25 类稀疏向量间交集求和），与 sim_d 同量纲相加。
        空索引直接返回 (空数组, [])——调用方据此短路。"""
        if self.index is None:
            return np.array([]), []
        index_norm = norm(self.index, axis=1)
        xq_d_norm = norm(vector)
        sim_d = np.squeeze(np.dot(self.index, vector.T)) / (index_norm * xq_d_norm)
        sim_s = np.array(self._sparse_index_dot_product(sparse_vector))
        total_sim = sim_d + sim_s
        top_k = min(top_k, total_sim.shape[0])
        # argpartition 只保证前 k 个在左侧，返回的索引无序——必须按分数降序重排，
        # 否则调用方拿到的不是真正 top-1/top-k（见 test_query_single_vector：2 元素时
        # 最小分反而排在 names[0]）。降序排列保证结果可作排名直接消费。
        idx = np.argpartition(total_sim, -top_k)[-top_k:]
        idx = idx[np.argsort(total_sim[idx])[::-1]]
        return total_sim[idx], list(self.routes[idx])

    def _sparse_index_dot_product(self, xq_s: dict[int, float] | None) -> list[float]:
        """对齐同名方法：query 稀疏向量与每个 doc 稀疏向量点积。

        逐 doc 遍历 query 的 token 取 doc 权重求和——query 稀疏向量通常
        远短于 doc 向量，以 query 为外循环只遍历命中 token，更快。"""
        if not xq_s or self.sparse_index is None:
            return [0.0] * (len(self.sparse_index) if self.sparse_index else 0)
        return [sum(w * doc.get(tok, 0.0) for tok, w in xq_s.items())
                for doc in self.sparse_index]
