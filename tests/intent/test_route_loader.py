# tests/intent/test_route_loader.py
import pytest
from pathlib import Path
from paperflow.core.intent.route_loader import load_routes
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
