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
        from paperflow.core.intent.intent_schema import IntentType
        routes = load_routes(Path("data/intents/routes.yaml"))
        assert len(routes) == 13
        names = {r.name for r in routes}
        assert names == {t.value for t in IntentType}

    def test_ask_question_covers_read_intent(self):
        """回归：ask_question 必须有"阅读论文"例句（否则"阅读…论文"误路由到 generate_note）。

        2026-08-06 实测：ask_question 原本零阅读例句，用户输入
        "阅读 <pdf 路径> 这篇论文" 被 HybridRouter 按 argmax 判为 generate_note（0.676），
        supervisor 据此 spawn 了 writer 而非 qa-agent。补阅读例句后
        该查询正确路由到 ask_question（0.713）。此测试守护数据形状，防例句被误删。
        """
        routes = load_routes(Path("data/intents/routes.yaml"))
        ask = next(r for r in routes if r.name == "ask_question")
        assert any("阅读" in u or u.startswith("读") for u in ask.utterances)


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
        "  - query: 帮我订个外卖\n"
        "    intent: out_of_scope\n"
        "    hard: true\n",
        encoding="utf-8",
    )
    items = load_eval(path)
    assert items == [
        ("帮我找找 circRNA 论文", "search_paper", False),
        ("帮我订个外卖", "out_of_scope", True),
    ]


def test_load_eval_rejects_invalid_intent(tmp_path):
    path = tmp_path / "eval.yaml"
    path.write_text("eval:\n  - query: x\n    intent: not_a_real_intent\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_eval(path)


def test_eval_disjoint_from_routes():
    """held-out 泛化保证：eval 查询不得与 routes 例句重复（否则测的是记忆不是泛化）。

    用生产文件默认路径断言——这是版本化验收契约的一部分（spec §4.7.1）。
    13 路由全参与（含 general）：Task 4 改写与 general 占位语料逐字重合的负样本
    （「好的」「嗯嗯」→ 非重合表述），恢复 general 也严格逐字不相交的完整契约。
    """
    route_utterances = {u for r in load_routes() for u in r.utterances}
    eval_items = load_eval()
    overlap = [q for q, _, _ in eval_items if q in route_utterances]
    assert overlap == [], f"eval 与 routes 相交 {len(overlap)} 条: {overlap[:5]}"


def test_eval_covers_all_intents():
    """eval 集契约：覆盖全部 13 类意图 + 规模下限（够统计意义，门槛才可信）。"""
    from paperflow.core.intent.intent_schema import IntentType
    eval_items = load_eval()
    labels = {label for _, label, _ in eval_items}
    assert {t.value for t in IntentType} <= labels
    assert len(eval_items) >= 100          # 规模下限：够统计意义


def test_routes_cover_new_taxonomy_13():
    """R1-R4：13 routes，新 route ≥8 语料，general ≤5 占位。"""
    from paperflow.core.intent.intent_schema import IntentType
    routes = load_routes()
    names = {r.name for r in routes}
    assert names == {t.value for t in IntentType}          # R1+R2：13 route，名合法
    utt = {r.name: len(r.utterances) for r in routes}
    new_intents = {"set_research_topic", "analyze_paper", "refine_query",
                   "switch_topic", "chitchat", "out_of_scope", "help", "feedback"}
    for n in new_intents:
        assert utt[n] >= 8, f"{n} 语料不足 8 条"             # R3
    assert utt["general"] <= 5                               # R4


def test_manage_memory_covers_reading_list():
    """待读清单折叠进 manage_memory：语料须含增删查待读的表述。"""
    routes = load_routes()
    mm = next(r for r in routes if r.name == "manage_memory")
    joined = "".join(mm.utterances)
    assert "待读" in joined


def test_eval_covers_new_intents_with_hard_negatives():
    """Q2：8 个新意图各有 ≥10 条 held-out；硬负样本（hard: true 标记）占比 ≥30%。

    硬负样本=与其他意图近形的混淆样本（如「讲讲这篇论文讲的什么」标 analyze_paper
    但形似 ask_question）——否则 per-intent 门槛对新意图无约束，是"自己给自己打分"
    的漏洞。"""
    items = load_eval()                    # 3 元组 (query, label, is_hard)
    labels = [l for _, l, _ in items]
    per = {l: labels.count(l) for l in set(labels)}
    new_intents = {"set_research_topic", "analyze_paper", "refine_query",
                   "switch_topic", "chitchat", "out_of_scope", "help", "feedback"}
    for n in new_intents:
        assert per.get(n, 0) >= 10, f"{n} held-out 样本 <10"
    hard = sum(1 for _, _, h in items if h)
    assert hard >= int(len(items) * 0.30), "硬负样本占比 <30%"
