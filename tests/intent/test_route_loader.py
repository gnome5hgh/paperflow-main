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
        """生产路径：data/intents/routes.yaml 必须可加载。

        含 general route——gate 实证驱动的修订：fit 无 general 负样本收敛到 0.0 阈值
        （pass-all），general 永不产生；加 general route 让 fit 学会拒绝（见 spec §4.7）。
        """
        routes = load_routes(Path("data/intents/routes.yaml"))
        assert len(routes) >= 5
        names = {r.name for r in routes}
        assert names == {"search_paper", "generate_note", "ask_question",
                         "manage_memory", "general"}


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


def test_eval_disjoint_from_routes():
    """held-out 泛化保证：eval 查询不得与 routes 例句重复（否则测的是记忆不是泛化）。

    用生产文件默认路径断言——这是版本化验收契约的一部分（spec §4.7.1）。
    """
    routes = load_routes()
    route_utterances = {u for r in routes for u in r.utterances}
    eval_items = load_eval()
    overlap = [q for q, _ in eval_items if q in route_utterances]
    assert overlap == [], f"eval 与 routes 相交 {len(overlap)} 条: {overlap[:5]}"


def test_eval_covers_all_intents():
    """eval 集契约：覆盖全部 5 类意图 + 规模下限（够统计意义，门槛才可信）。"""
    eval_items = load_eval()
    labels = {label for _, label in eval_items}
    assert {"search_paper", "generate_note", "ask_question", "manage_memory", "general"} <= labels
    assert len(eval_items) >= 100          # 规模下限：够统计意义
