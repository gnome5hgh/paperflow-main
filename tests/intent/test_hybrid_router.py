import numpy as np
import pytest
from paperflow.core.intent.schema import Route, RouteChoice
from paperflow.core.intent.hybrid_router import HybridRouter
from paperflow.core.intent.dense_encoder import FixedDenseEncoder
from paperflow.core.intent.bm25_encoder import BM25Encoder


def make_router(routes=None, alpha=0.3, top_k=5, thresholds=None):
    routes = routes or [
        Route(name="search_paper", utterances=["下载最新论文", "搜索 circRNA 文献"]),
        Route(name="generate_note", utterances=["把这篇论文整理成笔记", "写一份笔记"]),
        Route(name="ask_question", utterances=["circRNA 的机制是什么", "解释一下这个公式"]),
    ]
    router = HybridRouter(
        encoder=FixedDenseEncoder(dim=64),
        routes=routes, alpha=alpha, top_k=top_k,
    )
    if thresholds:
        router._update_thresholds(thresholds)
    return router


class TestAdd:
    def test_initial_add(self):
        router = make_router()
        assert len(router.routes) == 3
        assert router.index.index is not None
        assert router.index.index.shape[0] == 6    # 3 routes × 2 utterances

    def test_second_add_dimensions_match(self):
        """🔴 回归：第二次 add 索引维度必须匹配（fit 全部累积、入索引仅新增）。"""
        router = make_router()
        router.add(Route(name="manage_memory", utterances=["我读过哪些论文", "显示阅读记录"]))
        assert len(router.routes) == 4
        assert router.index.index.shape[0] == 8    # 6 + 2
        assert list(router.index.routes).count("manage_memory") == 2


class TestCall:
    def test_returns_route_choice(self):
        router = make_router()
        choice = router("下载最新论文")
        assert choice is not None
        assert choice.name == "search_paper"
        assert choice.similarity_score is not None

    def test_returns_single_choice_not_list(self):
        router = make_router()
        choice = router("下载最新论文")
        assert isinstance(choice, RouteChoice)     # ours: 单个（对照脚本按 impl 适配）
        assert not isinstance(choice, list)

    def test_threshold_filters(self):
        router = make_router(thresholds={"search_paper": 0.99,
                                         "generate_note": 0.99,
                                         "ask_question": 0.99})
        choice = router("完全无关的随机文本测试")
        # 高分阈值下大概率不命中（固定向量随机性下 0.99 极高）
        assert choice is None or choice.similarity_score >= 0.99


class TestScores:
    def test_returns_sorted_scores(self):
        router = make_router()
        scores = router.scores("下载最新论文", k=3)
        assert isinstance(scores, list)
        assert all(isinstance(t, tuple) and len(t) == 2 for t in scores)
        # 降序
        assert scores[0][1] >= scores[-1][1]


class TestFitEvaluate:
    def test_fit_runs_and_evaluate_returns_accuracy(self):
        router = make_router()
        X = ["下载最新论文", "搜索 circRNA 文献",
             "把这篇论文整理成笔记", "写一份笔记",
             "circRNA 的机制是什么", "解释一下这个公式",
             "下载新论文", "整理笔记", "机制是什么"]
        y = ["search_paper", "search_paper",
             "generate_note", "generate_note",
             "ask_question", "ask_question",
             "search_paper", "generate_note", "ask_question"]
        router.fit(X, y, max_iter=10)
        acc = router.evaluate(X, y)
        assert 0.0 <= acc <= 1.0
        assert isinstance(acc, float)

    def test_fit_improves_or_keeps_accuracy(self):
        """fit 后 accuracy 不劣于 fit 前（best 保留语义）。"""
        router = make_router()
        X = ["下载最新论文", "搜索 circRNA 文献",
             "把这篇论文整理成笔记", "写一份笔记",
             "circRNA 的机制是什么", "解释一下这个公式"]
        y = ["search_paper", "search_paper",
             "generate_note", "generate_note",
             "ask_question", "ask_question"]
        before = router.evaluate(X, y)
        router.fit(X, y, max_iter=20)
        after = router.evaluate(X, y)
        assert after >= before


class TestThresholds:
    def test_get_thresholds_uses_route_or_global(self):
        router = make_router()
        router.score_threshold = 0.5
        router.routes[0].score_threshold = 0.7
        thresholds = router.get_thresholds()
        assert thresholds["search_paper"] == 0.7    # route 阈值优先
        assert thresholds["generate_note"] == 0.5   # 全局兜底
