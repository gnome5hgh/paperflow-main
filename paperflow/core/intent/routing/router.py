# paperflow/core/intent/routing/router.py
"""混合路由器：稀疏（BM25）+ 稠密双路召回、按路由阈值裁决。

只做静态意图路由：本地内存索引、同步调用，一次查询返回单个 RouteChoice
（命中返回路由名与融合分数；未命中返回 None，交由管线走 LLM 兜底）。
"""
import random

import numpy as np

from paperflow.core.intent.schemas.route import Route, RouteChoice
from paperflow.core.intent.encoders.bm25 import BM25Encoder
from paperflow.core.intent.encoders.index import HybridLocalIndex


class HybridRouter:
    """混合路由器：稠密与稀疏按 alpha 凸组合打分、按路由阈值裁决。

    alpha=0.3 为默认稠密权重（稀疏权重为 1-alpha）；fit 只调阈值，不调 alpha。
    打分完全确定（无随机性）——路由未命中时，管线会把本路由器的近失候选分数
    注入 LLM 兜底 prompt，让 LLM 在路由先验上确认或改判，而非盲猜。"""

    def __init__(self, encoder, sparse_encoder: BM25Encoder | None = None,
                 routes: list[Route] | None = None,
                 index: HybridLocalIndex | None = None,
                 top_k: int = 5, alpha: float = 0.3):
        self.encoder = encoder
        self.sparse_encoder = sparse_encoder or BM25Encoder()
        self.index = index or HybridLocalIndex()
        self.routes: list[Route] = []
        self.top_k = top_k
        self.alpha = alpha
        self.score_threshold: float | None = None
        if routes:
            self.add(routes)

    def add(self, routes) -> None:
        """加入一批路由并编码入索引。

        ① fit 用全部累积路由（self.routes）——语料统计要覆盖历史所有样本；
        ② 编码入索引只用本次新增路由——维度匹配：新增 utterances 数 == 新增
        route 名数。若入索引用全部累积而 route 名只给新增，第二次 add 时
        np.concatenate 长度不匹配崩溃。"""
        if isinstance(routes, Route):
            routes = [routes]
        self.routes.extend(routes)
        # ① fit：用全部累积路由，语料统计覆盖历史所有样本
        all_utterances = [u for r in self.routes for u in r.utterances]
        self.sparse_encoder.fit(all_utterances)
        # ② 编码入索引：只用本次新增，与新增 route 名一一对应（维度匹配）
        new_utterances = [u for r in routes for u in r.utterances]
        dense_emb = np.array(self.encoder(new_utterances))
        sparse_emb = self.sparse_encoder.encode_documents(new_utterances)
        dense_scaled, sparse_scaled = self._convex_scaling(dense_emb, sparse_emb)
        self.index.add(
            embeddings=dense_scaled.tolist(),
            routes=[r.name for r in routes for _ in r.utterances],
            utterances=new_utterances,
            sparse_embeddings=sparse_scaled,
        )

    def _convex_scaling(self, dense, sparse):
        """按 alpha 凸组合缩放：dense × alpha，sparse × (1-alpha)。"""
        scaled_dense = np.array(dense) * self.alpha
        scaled_sparse = [{k: v * (1 - self.alpha) for k, v in d.items()}
                         for d in sparse]
        return scaled_dense, scaled_sparse

    def __call__(self, text: str | None = None,
                 vector: np.ndarray | None = None,
                 sparse_vector: dict[int, float] | None = None,
                 simulate_static: bool = False) -> RouteChoice | None:
        """一次查询的路由判定：编码 → 融合查询 → 按路由聚合打分 → 阈值裁决。

        :param simulate_static: 预留参数，静态路由下无分支。"""
        if vector is None:
            if text is None:
                raise ValueError("Either text or vector must be provided")
            dense_s, sparse_s = self._convex_scaling(
                np.array(self.encoder([text])),
                self.sparse_encoder([text]),
            )
            vector = dense_s[0]
            sparse_vector = sparse_s[0] if sparse_s else None
        scores, route_names = self.index.query(vector=vector,
                                               top_k=self.top_k,
                                               sparse_vector=sparse_vector)
        query_results = [{"route": d, "score": s}
                         for d, s in zip(route_names, scores)]
        scored_routes = self._score_routes(query_results)
        return self._pass_routes(scored_routes, simulate_static)

    def scores(self, query: str, k: int = 3) -> list[tuple[str, float]]:
        """返回 top_k 覆盖到的路由的融合分数（含未通过阈值的），降序——供 LLM 兜底参考。

        ⚠️ 注意：受 index.query(top_k=self.top_k) 限制，只覆盖 top_k 条 utterances
        命中的路由——不是全部路由的全局视图（与 __call__ 同一数据源）。"""
        dense_s, sparse_s = self._convex_scaling(
            np.array(self.encoder([query])),
            self.sparse_encoder([query]),
        )
        scores, route_names = self.index.query(vector=dense_s[0],
                                               top_k=self.top_k,
                                               sparse_vector=(sparse_s[0] if sparse_s else None))
        query_results = [{"route": d, "score": s}
                         for d, s in zip(route_names, scores)]
        scored = self._score_routes(query_results)
        return [(name, float(score)) for name, score, _ in scored[:k]]

    def _score_routes(self, query_results: list[dict]) -> list[tuple[str, float, list[float]]]:
        """按 route 分组聚合：取 mean 作为该路由的融合分数，按分数降序排列。"""
        scores_by_class: dict[str, list[float]] = {}
        for r in query_results:
            scores_by_class.setdefault(r["route"], []).append(r["score"])
        total = [(route, float(np.mean(scores)), scores)
                 for route, scores in scores_by_class.items()]
        total.sort(key=lambda x: x[1], reverse=True)
        return total

    def _pass_routes(self, scored_routes, simulate_static: bool) -> RouteChoice | None:
        """阈值裁决：按分数降序遍历，返回第一个过阈值的路由。

        route 自身阈值优先，否则用全局 score_threshold；全部未过阈值返回 None
        （未命中，交由管线走 LLM 兜底）。simulate_static 是预留参数，静态路由
        下无分支。"""
        for route_name, total_score, _scores in scored_routes:
            route = self.get(route_name)
            if route is None:
                continue
            threshold = (route.score_threshold if route.score_threshold is not None
                         else self.score_threshold)
            passed = total_score >= threshold if threshold is not None else True
            if passed:
                return RouteChoice(name=route_name, similarity_score=total_score)
        return None

    def get(self, name: str) -> Route | None:
        """按名称查找路由，未找到返回 None。"""
        return next((r for r in self.routes if r.name == name), None)

    def get_thresholds(self) -> dict[str, float]:
        """返回每个路由当前生效的阈值（路由自身阈值优先，否则用全局阈值）。"""
        return {r.name: (r.score_threshold if r.score_threshold is not None
                         else (self.score_threshold or 0.0))
                for r in self.routes}

    def _update_thresholds(self, route_thresholds: dict[str, float]) -> None:
        """按名称批量覆写路由的 score_threshold（fit 训练时使用）。"""
        for r in self.routes:
            if r.name in route_thresholds:
                r.score_threshold = route_thresholds[r.name]

    def fit(self, X: list[str], y: list[str],
            batch_size: int = 500, max_iter: int = 500) -> None:
        """在给定样本上训练路由阈值：迭代 max_iter 次阈值随机搜索，保留最佳准确率。

        每轮对每个路由的当前阈值在 ±0.8 范围内 100 等分随机采样一个新阈值，
        用样本评估准确率，最终写回准确率最高的一组阈值（阈值是每路由独立的，
        见 load_eval 对硬负样本占比的要求）。"""
        Xq_d = np.concatenate([self.encoder(X[i:i + batch_size])
                               for i in range(0, len(X), batch_size)]) if X else np.array([])
        Xq_s = [s for b in [self.sparse_encoder(X[i:i + batch_size])
                            for i in range(0, len(X), batch_size)] for s in b]
        best_acc = self._vec_evaluate(Xq_d, Xq_s, y)
        best_thresholds = self.get_thresholds()
        for _ in range(max_iter):
            thresholds = self._threshold_random_search(search_range=0.8)
            self._update_thresholds(thresholds)
            acc = self._vec_evaluate(Xq_d, Xq_s, y)
            if acc > best_acc:
                best_acc = acc
                best_thresholds = thresholds
        self._update_thresholds(best_thresholds)

    def _threshold_random_search(self, search_range: float) -> dict[str, float]:
        """对每个路由在当前阈值附近 ±search_range 范围内 100 等分随机采样一个新阈值。"""
        result = {}
        for route, threshold in self.get_thresholds().items():
            values = np.linspace(max(threshold - search_range, 0.0),
                                 min(threshold + search_range, 1.0), num=100)
            result[route] = float(random.choice(values))
        return result

    def evaluate(self, X: list[str], y: list[str], batch_size: int = 500) -> float:
        """在给定样本上评估路由准确率（判定结果与真值标签一致的比例）。"""
        Xq_d = np.concatenate([self.encoder(X[i:i + batch_size])
                               for i in range(0, len(X), batch_size)]) if X else np.array([])
        Xq_s = [s for b in [self.sparse_encoder(X[i:i + batch_size])
                            for i in range(0, len(X), batch_size)] for s in b]
        return self._vec_evaluate(Xq_d, Xq_s, y)

    def _vec_evaluate(self, Xq_d, Xq_s, y: list[str]) -> float:
        """批量评估路由准确率：逐样本用 simulate_static 路由判定，与真值标签比对，返回一致比例。"""
        correct = 0
        for xq_d, xq_s, target in zip(Xq_d, Xq_s, y):
            choice = self(vector=xq_d, sparse_vector=xq_s, simulate_static=True)
            if choice is not None and choice.name == target:
                correct += 1
        return correct / max(len(Xq_d), 1)
