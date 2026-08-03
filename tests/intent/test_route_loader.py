# tests/intent/test_route_loader.py
import pytest
from pathlib import Path
from paperflow.core.intent.route_loader import load_routes, load_eval, save_thresholds
from paperflow.core.intent.schema import Route


def write_routes(tmp_path, content):
    p = tmp_path / "routes.yaml"
    p.write_text(content, encoding="utf-8")
    return p


class TestLoadRoutes:
    def test_loads_valid_file(self, tmp_path):
        p = write_routes(tmp_path, """
routes:
  - name: search_paper
    utterances: ["下载最新论文", "搜索 circRNA 文献"]
  - name: generate_note
    utterances: ["把这篇论文整理成笔记"]
""")
        routes = load_routes(p)
        assert len(routes) == 2
        assert routes[0].name == "search_paper"
        assert len(routes[0].utterances) == 2
        assert routes[0].score_threshold is None

    def test_rejects_unknown_route_name(self, tmp_path):
        p = write_routes(tmp_path, """
routes:
  - name: not_an_intent
    utterances: ["测试"]
""")
        with pytest.raises(ValueError, match="IntentType"):
            load_routes(p)

    def test_rejects_empty_utterances(self, tmp_path):
        p = write_routes(tmp_path, """
routes:
  - name: search_paper
    utterances: []
""")
        with pytest.raises(ValueError, match="utterances"):
            load_routes(p)

    def test_loads_real_routes_file(self):
        """生产路径：data/intents/routes.yaml 必须可加载。"""
        routes = load_routes(Path("data/intents/routes.yaml"))
        assert len(routes) >= 4
        names = {r.name for r in routes}
        assert names == {"search_paper", "generate_note", "ask_question",
                         "manage_memory"}


def test_save_thresholds_round_trip(tmp_path):
    """标定写回 → load 读回：per-route 阈值不丢、utterances 不丢。"""
    path = tmp_path / "routes.yaml"
    routes = [
        Route(name="search_paper",
              utterances=["搜索 circRNA 文献", "下载最新论文"], score_threshold=0.62),
        Route(name="generate_note",
              utterances=["写一份笔记", "把这篇整理成笔记"], score_threshold=0.55),
    ]
    save_thresholds(path, routes)
    loaded = load_routes(path)
    assert [r.score_threshold for r in loaded] == [0.62, 0.55]
    assert loaded[0].utterances == routes[0].utterances
    assert loaded[1].utterances == routes[1].utterances


def test_save_thresholds_omits_none(tmp_path):
    """score_threshold=None 时不写该字段（保持最小 diff；load 回落默认 None）。"""
    path = tmp_path / "routes.yaml"
    save_thresholds(path, [Route(name="general", utterances=["你好"])])
    raw = path.read_text(encoding="utf-8")
    assert "score_threshold" not in raw
    loaded = load_routes(path)
    assert loaded[0].score_threshold is None


def test_load_eval_parses_and_validates(tmp_path):
    path = tmp_path / "eval.yaml"
    path.write_text(
        "eval:\n"
        "  - query: 帮我找找 circRNA 论文\n"
        "    intent: search_paper\n"
        "  - query: 你好呀\n"
        "    intent: general\n",
        encoding="utf-8",
    )
    items = load_eval(path)
    assert items == [("帮我找找 circRNA 论文", "search_paper"), ("你好呀", "general")]


def test_load_eval_rejects_invalid_intent(tmp_path):
    path = tmp_path / "eval.yaml"
    path.write_text("eval:\n  - query: x\n    intent: not_a_real_intent\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_eval(path)
