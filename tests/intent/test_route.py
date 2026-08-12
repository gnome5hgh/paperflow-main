# tests/intent/test_route.py
"""Route / RouteChoice 契约测试：字段默认值、per-route 阈值、空 RouteChoice 语义（spec Section 5）。"""

from paperflow.core.intent.schemas.route import Route, RouteChoice


def test_route_required_fields():
    # dataclass：仅 name 即可构造，其余字段有默认值
    r = Route(name="search_paper")
    assert r.name == "search_paper"
    assert r.utterances == []
    assert r.score_threshold is None


def test_route_with_utterances():
    r = Route(name="search_paper", utterances=["搜索 xxx 论文", "帮我查一篇论文"])
    assert r.utterances == ["搜索 xxx 论文", "帮我查一篇论文"]


def test_route_with_threshold():
    # per-route 阈值覆盖路由器全局阈值
    r = Route(name="ask_question", utterances=["这是什么"], score_threshold=0.35)
    assert r.score_threshold == 0.35


def test_route_mutable_defaults_isolated():
    # dataclass 可变默认值用 field(default_factory=list)——实例之间不共享列表
    a = Route(name="a", utterances=["x"])
    b = Route(name="b", utterances=["y"])
    a.utterances.append("z")
    assert a.utterances == ["x", "z"]
    assert b.utterances == ["y"]


def test_route_choice_empty_default():
    # 未命中任何路由时返回空 RouteChoice（name=None，与 semantic-router 一致）
    rc = RouteChoice()
    assert rc.name is None
    assert rc.similarity_score is None


def test_route_choice_with_values():
    rc = RouteChoice(name="search_paper", similarity_score=0.87)
    assert rc.name == "search_paper"
    assert rc.similarity_score == 0.87
