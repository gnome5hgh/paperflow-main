"""稠密 + 稀疏双路融合索引（内存版），服务混合路由的向量查询。"""
import numpy as np
from numpy.linalg import norm


class HybridLocalIndex:
    """稠密 + 稀疏双索引（内存版）。

    融合查询 = 稠密余弦相似度（sim_d）+ 稀疏点积（sim_s），
    用 argpartition 取 top_k（不排序全量，O(n) 复杂度取前 k）。
    稀疏索引存 {token_id: weight} 字典列表，点积时逐 doc 求和——
    稀疏向量维度远大于稠密 dim，字典表示避免零值浪费。"""

    def __init__(self):
        """初始化空索引，各属性为 None 表示未填充。"""
        self.index: np.ndarray | None = None        # 稠密向量矩阵，形状 (n, dim)，每行是一个已缩放（乘以 alpha，在 router.py 中实现）的稠密向量。
        self.sparse_index: list[dict] | None = None  # 稀疏向量列表 [{token_id: weight}]，权重已缩放（乘以 1-alpha，在 router.py 中实现）
        self.routes: np.ndarray | None = None       # 路由标签数组，形状 (n_samples,)，存储每个样本对应的意图路由名（str）
        self.utterances: np.ndarray | None = None   # 原始示例句数组，形状 (n_samples,)，仅用于调试或追溯，不参与查询。

    def add(self, embeddings, routes, utterances, sparse_embeddings) -> None:
        """加入一批样本：首次调用直接初始化，之后 np.concatenate 追加。

        追加保持 index 是单一 ndarray，query 里的 norm/dot 可向量化一次性算完，不逐行循环。

        Args:
            embeddings: 稠密向量矩阵，形状 (n, dim)，每行是一个稠密向量。
            routes: 对应的路由标签列表，长度 n，每个元素是意图名（str）。
            utterances: 对应的原始文本列表，长度 n，用于调试。
            sparse_embeddings: 稀疏向量列表 [{token_id: weight}]，每个元素为 {token_id: weight} 字典。
        """
        # 将输入统一转为 numpy 数组（稠密向量、路由、文本）
        embeds = np.array(embeddings)
        routes_arr = np.array(routes)
        utts_arr = np.array(utterances)

        if self.index is None:
            # 首次添加：直接赋值
            self.index = embeds
            # 确保 sparse_embeddings 中的每个字典是独立副本（避免外部修改影响内部）
            self.sparse_index = [dict(x) for x in sparse_embeddings]
            self.routes = routes_arr
            self.utterances = utts_arr
        else:
            # 追加：沿行拼接，保持各数组长度一致
            self.index = np.concatenate([self.index, embeds])
            self.sparse_index.extend(dict(x) for x in sparse_embeddings)
            self.routes = np.concatenate([self.routes, routes_arr])
            self.utterances = np.concatenate([self.utterances, utts_arr])

    def query(self, vector, top_k: int = 5,
              sparse_vector: dict[int, float] | None = None
              ) -> tuple[np.ndarray, list[str]]:
        """融合查询：sim_d（余弦）+ sim_s（稀疏点积）→ argpartition 取 top_k。

        sim_d：余弦相似度，除以各行范数 * query 范数（防止 query 未归一化）。
        sim_s：稀疏点积（BM25 类稀疏向量间交集求和），与 sim_d 同量纲相加。
        空索引直接返回 (空数组, [])——调用方据此短路。

        Args:
            vector: 查询稠密向量 (dim, )。
            top_k: 返回的 top 结果数。
            sparse_vector: 查询稀疏向量字典 {token_id: weight}，可为 None。

        Returns:
            (总相似度分数数组按降序排列, 对应的路由标签列表)，长度 ≤ top_k。
        """
        if self.index is None:
            return np.array([]), []

        # 计算稠密余弦相似度（向量化计算）
        index_norm = norm(self.index, axis=1)
        xq_d_norm = norm(vector)
        # 点积：self.index 是 (n, dim)，vector 是 (dim,)，dot 得 (n,)
        # 除以 (index_norm * xq_d_norm) 得到余弦相似度
        sim_d = np.squeeze(np.dot(self.index, vector.T)) / (index_norm * xq_d_norm)

        # 计算稀疏点积（逐 doc 遍历 query 的 token）
        sim_s = np.array(self._sparse_index_dot_product(sparse_vector))
        total_sim = sim_d + sim_s

        top_k = min(top_k, total_sim.shape[0])

        # argpartition 取分数最高的 top_k 个索引（无序）
        idx = np.argpartition(total_sim, -top_k)[-top_k:]
        # 按分数降序重排，确保返回真正的 top-1 到 top-k
        idx = idx[np.argsort(total_sim[idx])[::-1]]
        return total_sim[idx], list(self.routes[idx])

    def _sparse_index_dot_product(self, xq_s: dict[int, float] | None) -> list[float]:
        """query 稀疏向量与每个 doc 稀疏向量的点积。

        逐 doc 遍历 query 的 token 取 doc 权重求和——query 稀疏向量通常
        远短于 doc 向量，以 query 为外循环只遍历命中 token，更快。

        Args:
            xq_s: 查询稀疏向量字典，可能为 None 或空。

        Returns:
            float 列表，长度等于稀疏索引文档数；若索引为空或查询为空则全零。
        """
        if not xq_s or self.sparse_index is None:
            # 若查询稀疏向量缺失或索引未初始化，返回与文档数等长的零列表
            return [0.0] * (len(self.sparse_index) if self.sparse_index else 0)

        # 对每个文档，累加 query 中每个 token 在文档中的权重
        return [sum(w * doc.get(tok, 0.0) for tok, w in xq_s.items())
                for doc in self.sparse_index]
