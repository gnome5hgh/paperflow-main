# tests/intent/test_route.py
"""Route / RouteChoice 契约测试：字段默认值、per-route 阈值、空 RouteChoice 语义。"""

from paperflow.core.intent.route import Route, RouteChoice


def test_route_required_fields():
    # 仅 name + utterances 即可构造（其余字段有默认值）
    r = Route(name="search_paper", utterances=["搜索 xxx 论文", "帮我查一篇论文"])
    assert r.name == "search_paper"
    assert r.utterances == ["搜索 xxx 论文", "帮我查一篇论文"]
    assert r.description is None
    assert r.score_threshold is None
    assert r.metadata == {}


def test_route_with_optional_fields():
    # per-route 阈值 + 调度映射元数据
    r = Route(
        name="download_paper",
        utterances=["下载这篇 PDF"],
        description="下载论文 PDF",
        score_threshold=0.35,
        metadata={"subagent": "search-paper", "download": True},
    )
    assert r.score_threshold == 0.35
    assert r.metadata["subagent"] == "search-paper"
    assert r.metadata["download"] is True


def test_route_mutable_defaults_isolated():
    # pydantic v2 对可变默认值做深拷贝，实例之间不应共享同一份列表/字典
    a = Route(name="a", utterances=["x"])
    b = Route(name="b", utterances=["y"])
    a.utterances.append("z")
    a.metadata["k"] = "v"
    assert a.utterances == ["x", "z"]
    assert b.utterances == ["y"]
    assert b.metadata == {}


def test_route_choice_empty_default():
    # 未命中任何路由时返回空 RouteChoice（name=None，与 semantic-router 一致）
    rc = RouteChoice()
    assert rc.name is None
    assert rc.function_call is None
    assert rc.similarity_score is None


def test_route_choice_with_values():
    rc = RouteChoice(name="search_paper", similarity_score=0.87)
    assert rc.name == "search_paper"
    assert rc.similarity_score == 0.87
    assert rc.function_call is None
