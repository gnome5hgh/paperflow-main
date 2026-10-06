# paperflow/core/intent/routing/router.py
"""混合路由器：稀疏（BM25）+ 稠密双路召回、按路由阈值裁决。

角色（裁判）：混合路由三层分工为 编码器（翻译：文本 → 向量）→
HybridLocalIndex（记分员：裸相似度）→ 本类（判定：分数 → 意图结论）。

本类持有全部三者——__init__ 注入稠密编码器、默认新建 BM25Encoder 与 HybridLocalIndex。

一次查询五步（__call__）：
  ① 编码   text → 稠密向量（encoder）+ 稀疏向量（BM25Encoder）
  ② 加权   _convex_scaling：dense × alpha（稠密权重），sparse × (1-alpha)
  ③ 检索   index.query 拿 top_k 裸分数（index 只算数学，不懂判定）
  ④ 聚合   _score_routes：top_k 候选按路由名分组取均值
  ⑤ 裁决   _pass_routes：过阈值（路由专属优先，否则全局）→ RouteChoice；全不过 → None，交由管线走 LLM 兜底

alpha 语义：稀疏抓关键词精确匹配（说什么词命中什么意图）、稠密兜同义改写（换措辞也能命中）——alpha 为稠密路权重，生产值由 CLI 装配传 config.intent.router.alpha（唯一声明点 config.py），fit 只调阈值、不调 alpha。
fit()/scores() 是裁判的附属工具：前者随机搜索训练每路由阈值，后者给 LLM兜底提供近失候选。

只做静态意图路由：本地内存索引、同步调用，一次查询返回单个 RouteChoice
（命中返回路由名与融合分数；未命中返回 None，交由管线走 LLM 兜底）。
"""
import logging
import random

import numpy as np

from paperflow.core.intent.schemas.route import Route, RouteChoice
from paperflow.core.intent.encoders.bm25 import BM25Encoder
from paperflow.core.intent.encoders.index import HybridLocalIndex
from paperflow.core.intent.routing.vector_cache import (
    cache_key, load_cached_dense, save_cached_dense)

logger = logging.getLogger(__name__)

# ── fit 随机搜索超参（本文件 fit()/evaluate 消费） ─────────────────────────

#: fit/evaluate 的批编码大小。
#: - 值：500。
#: - 含义与单位：每批送入编码器的样本条数，避免内存过载（条）。
#: - 改它的后果：仅影响内存与耗时，不改变打分结果、无需重标定。
FIT_BATCH_SIZE = 500

#: fit 随机搜索的迭代次数。
#: - 值：500。
#: - 含义与单位：每轮为每个路由在当前阈值附近随机采样新阈值并评估准确率，
#:   迭代次数（轮）。
#: - 改它的后果：改变阈值搜索结果，需重跑 fit（routes.yaml 阈值随之为新产物）。
FIT_MAX_ITER = 500

#: fit 阈值随机搜索的采样半径。
#: - 值：0.8。
#: - 含义与单位：每个路由在 [当前阈值 - 0.8, 当前阈值 + 0.8] 内采样新阈值，
#:   截断至 [0,1]；与分数同量纲。
#: - 改它的后果：改变阈值搜索范围，需重跑 fit。
FIT_SEARCH_RANGE = 0.8

#: fit 阈值随机搜索的候选点数。
#: - 值：100。
#: - 含义与单位：每次采样时对搜索区间做 100 等分后随机取一点（点）。
#: - 改它的后果：改变阈值搜索粒度，需重跑 fit。
FIT_NUM_CANDIDATES = 100


class HybridRouter:
    """混合路由器：稠密与稀疏按 alpha 凸组合打分、按路由阈值裁决。

    alpha 为稠密路权重（稀疏权重 1-alpha）；生产值由 CLI 装配传
    config.intent.router.alpha（唯一声明点 config.py），fit 只调阈值、不调 alpha。
    打分完全确定（无随机性）——路由未命中时，管线会把本路由器的近失候选分数
    注入 LLM 兜底 prompt，让 LLM 在路由先验上确认或改判，而非盲猜。"""

    def __init__(self, encoder, top_k: int, alpha: float,
                 sparse_encoder: BM25Encoder | None = None,
                 routes: list[Route] | None = None,
                 index: HybridLocalIndex | None = None,
                 vector_cache_path: str | None = None):
        """初始化混合路由器。

        top_k/alpha 生产值来自 ``intent.router.*``（唯一声明点 config.py），
        由装配侧注入；测试代码可显式传其他值。

        Args:
            encoder: 稠密编码器（实现 __call__ 返回向量列表）。
            sparse_encoder: 稀疏 BM25 编码器，若未提供则新建默认实例。
            routes: 初始路由列表，可选。
            index: 双路索引实例，若未提供则新建。
            top_k: 检索召回时取 top_k 条候选语料（用于路由聚合）。
            alpha: 稠密分支的权重，稀疏分支权重为 (1 - alpha)。
            vector_cache_path: 路由语料稠密向量 npz 缓存路径；命中则跳过
                编码（零网络启动），未命中现算后回写。None 保持原行为。
        """
        self.encoder = encoder
        self.sparse_encoder = sparse_encoder or BM25Encoder()
        self.index = index or HybridLocalIndex()
        self.routes: list[Route] = []
        self.top_k = top_k
        self.alpha = alpha
        self.score_threshold: float | None = None # 全局路由通过阈值（可被路由自身覆盖）
        self.vector_cache_path = vector_cache_path
        self._dense_degraded = False   # 稠密降级只告警一次
        if routes:
            self.add(routes)

    @property
    def dense_degraded(self) -> bool:
        """稠密路是否已降级（只读；启动期 cli 据此打印黄字告警）。"""
        return self._dense_degraded

    def _static_dim(self) -> int | None:
        """编码器的**不发网络**维度（CloudEmbedder.dim_static）；未登记模型 None。

        测试替身（如 _DeadEncoder）无此属性时返回 None——其 dim 属性可能是
        纯静态的，但约定上降级路径只信 dim_static，避免任何潜在探测请求。
        """
        return getattr(self.encoder, "dim_static", None)

    def _cache_dim(self) -> int | None:
        """缓存键所需维度：静态映射优先（不发网络）；未登记模型才探测，失败返 None。

        未登记模型 + 断网时探测会抛——捕获后返回 None 让调用方跳过缓存，
        而不是让 add() 在 cache_key 处中断启动（启动永不因网络失败）。
        """
        static = self._static_dim()
        if static is not None:
            return static
        try:
            return self.encoder.dim
        except Exception:
            return None

    def _encode_dense(self, texts: list[str]) -> np.ndarray:
        """稠密编码 + 失败降级：云端不可达时退零向量（sim_d=0 → 纯稀疏判定）。

        降级是一次性告警而非每次刷屏——网络恢复后下次调用自然回到稠密路
        （每次调用都是独立 HTTP 请求，无熔断状态）。

        降级必须拿得到**不发网络**的静态维度（dim_static）才能凑出形状一致的
        零向量行。未登记模型断网时拿不到——不再静默退化为错误形状，而是抛出
        带清晰信息的错误（启动中断只有一个明确原因，而非掩盖成别处的怪异失败）。
        """
        try:
            return np.array(self.encoder(texts))
        except Exception as e:
            static_dim = self._static_dim()
            if static_dim is None:
                raise RuntimeError(
                    f"意图稠密编码不可用，且模型 "
                    f"{getattr(self.encoder, 'model_name', '?')!r} 未登记静态维度，"
                    f"无法降级为零向量：{e}") from e
            if not self._dense_degraded:
                self._dense_degraded = True
                logger.warning("意图稠密编码不可用（%s），降级为纯 BM25 稀疏路由；"
                               "网络恢复后自动回到混合路由", e)
            return np.zeros((len(texts), static_dim))

    def add(self, routes) -> None:
        """加入一批路由并编码入索引。

        ① fit 用全部累积路由（self.routes）——语料统计要覆盖历史所有样本；
        ② 编码入索引只用本次新增路由——维度匹配：新增 utterances 数 == 新增
        route 名数。若入索引用全部累积而 route 名只给新增，第二次 add 时
        np.concatenate 长度不匹配崩溃。

        Args:
            routes: 单个 Route 对象或 Route 列表。
        """
        if isinstance(routes, Route):
            routes = [routes]
        self.routes.extend(routes)

        # ① fit：用全部累积路由，语料统计覆盖历史所有样本
        all_utterances = [u for r in self.routes for u in r.utterances]
        self.sparse_encoder.fit(all_utterances)

        # ② 编码入索引：只用本次新增，与新增 route 名一一对应（维度匹配）
        new_utterances = [u for r in routes for u in r.utterances]
        dense_emb = None
        key = None
        if self.vector_cache_path:
            # 缓存键用本次新增语料（缓存条目与被编码批次严格一一对应）——
            # 生产装配是单次 add(全部路由)，此处即全量语料；若未来出现多次
            # add，第二次的键(新语料)与第一次不同，各存各的、互不污染。
            # 维度走 _cache_dim：静态映射优先，绝不在启动期因网络拉取维度而中断。
            _dim = self._cache_dim()
            if _dim is not None:
                key = cache_key(getattr(self.encoder, "model_name", ""),
                                _dim, new_utterances)
                dense_emb = load_cached_dense(self.vector_cache_path, key)
            # _dim 为 None（未登记模型且探测失败）：跳过缓存读写，直接现算——
            # 现算失败会由 _encode_dense 给出清晰错误（而非此处探测抛裸异常）。
        if dense_emb is None:
            dense_emb = self._encode_dense(new_utterances)
            # 仅真实编码成功才回写——降级零向量入缓存会把"断网"固化
            if self.vector_cache_path and key is not None and not self._dense_degraded:
                save_cached_dense(self.vector_cache_path, key,
                                  getattr(self.encoder, "model_name", ""), dense_emb)
        sparse_emb = self.sparse_encoder.encode_documents(new_utterances)
        dense_scaled, sparse_scaled = self._convex_scaling(dense_emb, sparse_emb)
        self.index.add(
            embeddings=dense_scaled.tolist(),
            routes=[r.name for r in routes for _ in r.utterances],
            utterances=new_utterances,
            sparse_embeddings=sparse_scaled,
        )

    def _convex_scaling(
            self,
            dense: np.ndarray,  # (n, dim) 稠密向量批次，每行一条
            sparse: list[dict[int, float]],  # 稀疏向量批次，每项 {token_id: 权重}
    ) -> tuple[np.ndarray, list[dict[int, float]]]:
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

        Args:
            text: 查询文本，若提供则使用编码器生成向量；否则必须提供 vector。
            vector: 预编码的稠密向量（已缩放），直接用于查询。
            sparse_vector: 预编码的稀疏向量（已缩放），直接用于查询。
            simulate_static: 预留参数，静态路由下无分支。

        Returns:
            若命中路由则返回 RouteChoice 对象（含路由名和融合分数），否则返回 None。

        Raises:
            ValueError: 当 text 和 vector 均为 None 时。
        """
        if vector is None:
            if text is None:
                raise ValueError("Either text or vector must be provided")

            # 在线编码并缩放
            dense_s, sparse_s = self._convex_scaling(
                self._encode_dense([text]),
                self.sparse_encoder([text]),
            )
            vector = dense_s[0]
            sparse_vector = sparse_s[0] if sparse_s else None

        # 从索引中检索 top_k 条候选语料，返回融合分数和对应的路由名
        # scores：np.ndarray (top_k,)，如：array([1.42, 1.18, 0.97, 0.85, 0.61])
        # route_names：list[str]，如：['search_paper', 'search_paper', 'search_paper', 'generate_note', 'ask_question']
        scores, route_names = self.index.query(vector=vector,
                                               top_k=self.top_k,
                                               sparse_vector=sparse_vector)

        # 构造查询结果列表
        # query_results：list[dict]，如：
        # [{"route": "search_paper", "score": 1.42},
        #  {"route": "search_paper", "score": 1.18},
        #  {"route": "search_paper", "score": 0.97},
        #  {"route": "generate_note", "score": 0.85},
        #  {"route": "ask_question",  "score": 0.61}]
        query_results = [{"route": d, "score": s}
                         for d, s in zip(route_names, scores)]

        # scored_routes：list[tuple[str, float, list[float]]]，如：
        # [("search_paper", 1.19, [1.42, 1.18, 0.97]),  # (1.42+1.18+0.97)/3
        #  ("generate_note", 0.85, [0.85]),  # 只命中 1 句 → 均值=它自己
        #  ("ask_question", 0.61, [0.61])]
        scored_routes = self._score_routes(query_results)
        return self._pass_routes(scored_routes, simulate_static)

    def scores(self, query: str, k: int = 3) -> list[tuple[str, float]]:
        """返回 top_k 覆盖到的路由的融合分数（含未通过阈值的），降序——供 LLM 兜底参考。

        ⚠️ 注意：受 index.query(top_k=self.top_k) 限制，只覆盖 top_k 条 utterances
        命中的路由——不是全部路由的全局视图（与 __call__ 同一数据源）。

        Args:
            query: 查询文本。
            k: 返回的路由数量（最多）。

        Returns:
            列表，每个元素为 (路由名, 融合分数)，按分数降序排列。
        """
        # 在线编码并缩放
        dense_s, sparse_s = self._convex_scaling(
            self._encode_dense([query]),
            self.sparse_encoder([query]),
        )

        # 从索引中检索 top_k 条候选语料，返回融合分数和对应的路由名
        # scores：np.ndarray (top_k,)，如：array([1.42, 1.18, 0.97, 0.85, 0.61])
        # route_names：list[str]，如：['search_paper', 'search_paper', 'search_paper', 'generate_note', 'ask_question']
        scores, route_names = self.index.query(vector=dense_s[0],
                                               top_k=self.top_k,
                                               sparse_vector=(sparse_s[0] if sparse_s else None))

        # 构造查询结果列表
        # query_results：list[dict]，如：
        # [{"route": "search_paper", "score": 1.42},
        #  {"route": "search_paper", "score": 1.18},
        #  {"route": "search_paper", "score": 0.97},
        #  {"route": "generate_note", "score": 0.85},
        #  {"route": "ask_question",  "score": 0.61}]
        query_results = [{"route": d, "score": s}
                         for d, s in zip(route_names, scores)]

        # scored：list[tuple[str, float, list[float]]]，如：
        # [("search_paper", 1.19, [1.42, 1.18, 0.97]),  # (1.42+1.18+0.97)/3
        #  ("generate_note", 0.85, [0.85]),  # 只命中 1 句 → 均值=它自己
        #  ("ask_question", 0.61, [0.61])]
        scored = self._score_routes(query_results)
        return [(name, float(score)) for name, score, _ in scored[:k]]

    def _score_routes(self, query_results: list[dict]) -> list[tuple[str, float, list[float]]]:
        """按 route 分组聚合：取 mean 作为该路由的融合分数，按分数降序排列。

        Args:
            query_results: 列表，每个元素含 "route" 和 "score"。

        Returns:
            列表，每个元素为 (路由名, 平均分, 原始分数列表)，按平均分降序。
        """
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
        下无分支。

        Args:
            scored_routes: _score_routes 的输出。
            simulate_static: 预留，无实际作用。

        Returns:
            RouteChoice 或 None。
        """
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
            batch_size: int = FIT_BATCH_SIZE, max_iter: int = FIT_MAX_ITER) -> None:
        """在给定样本上训练路由阈值：迭代 max_iter 次阈值随机搜索，保留最佳准确率。

        每轮对每个路由的当前阈值在 ±FIT_SEARCH_RANGE 范围内
        FIT_NUM_CANDIDATES 等分随机采样一个新阈值，
        用样本评估准确率，最终写回准确率最高的一组阈值（阈值是每路由独立的）。

        Args:
            X: 查询文本列表。
            y: 对应的真值路由名列表。
            batch_size: 批量编码大小，避免内存过载。
            max_iter: 随机搜索迭代次数。
        """
        Xq_d = np.concatenate([self.encoder(X[i:i + batch_size])
                               for i in range(0, len(X), batch_size)]) if X else np.array([])
        Xq_s = [s for b in [self.sparse_encoder(X[i:i + batch_size])
                            for i in range(0, len(X), batch_size)] for s in b]
        best_acc = self._vec_evaluate(Xq_d, Xq_s, y)
        best_thresholds = self.get_thresholds()
        for _ in range(max_iter):
            thresholds = self._threshold_random_search(search_range=FIT_SEARCH_RANGE)
            self._update_thresholds(thresholds)
            acc = self._vec_evaluate(Xq_d, Xq_s, y)
            if acc > best_acc:
                best_acc = acc
                best_thresholds = thresholds
        self._update_thresholds(best_thresholds)

    def _threshold_random_search(self, search_range: float) -> dict[str, float]:
        """对每个路由在当前阈值附近 ±search_range 范围内按 FIT_NUM_CANDIDATES 等分随机采样一个新阈值。

        Args:
            search_range: 采样半径（绝对值），阈值截断至 [0.0, 1.0]。

        Returns:
            {路由名: 新阈值} 字典。
        """
        result = {}
        for route, threshold in self.get_thresholds().items():
            values = np.linspace(max(threshold - search_range, 0.0),
                                 min(threshold + search_range, 1.0), num=FIT_NUM_CANDIDATES)
            result[route] = float(random.choice(values))
        return result

    def evaluate(self, X: list[str], y: list[str], batch_size: int = FIT_BATCH_SIZE) -> float:
        """在给定样本上评估路由准确率（判定结果与真值标签一致的比例）。

        Args:
            X: 查询文本列表。
            y: 对应的真值路由名列表。
            batch_size: 批量编码大小。

        Returns:
            准确率（0~1）。
        """
        Xq_d = np.concatenate([self.encoder(X[i:i + batch_size])
                               for i in range(0, len(X), batch_size)]) if X else np.array([])
        Xq_s = [s for b in [self.sparse_encoder(X[i:i + batch_size])
                            for i in range(0, len(X), batch_size)] for s in b]
        return self._vec_evaluate(Xq_d, Xq_s, y)

    def _vec_evaluate(self, Xq_d, Xq_s, y: list[str]) -> float:
        """批量评估路由准确率：逐样本用 simulate_static 路由判定，与真值标签比对，返回一致比例。

        Args:
            Xq_d: 稠密向量数组。
            Xq_s: 稀疏向量列表，与 Xq_d 一一对应。
            y: 真值路由名列表。

        Returns:
            准确率。
        """
        correct = 0
        for xq_d, xq_s, target in zip(Xq_d, Xq_s, y):
            choice = self(vector=xq_d, sparse_vector=xq_s, simulate_static=True)
            if choice is not None and choice.name == target:
                correct += 1
        return correct / max(len(Xq_d), 1)
