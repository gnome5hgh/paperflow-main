# paperflow/core/intent/routing/route_loader.py
"""意图知识库加载器——routes.yaml 是唯一知识库源（测试与生产共用路径）。"""
from pathlib import Path

import yaml

from paperflow.core.intent.schemas.route import Route
from paperflow.core.intent.schemas.intent import IntentType


def load_routes(path: Path = Path("data/intents/routes.yaml")) -> list[Route]:
    """yaml → [Route(name, utterances, score_threshold)]。只读加载。

    校验：① route 名必须在 IntentType 枚举中——否则 pipeline 的
    IntentType(choice.name) 会抛 ValueError 崩溃（routes.yaml 拼错/未同步枚举）
    ② utterances 非空——空列表 route 导致 fit([])（avg_doc_len 对空数组报错/NaN）"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    valid_names = {t.value for t in IntentType}
    routes = []
    for r in data["routes"]:
        if r["name"] not in valid_names:
            raise ValueError(f"route 名不在 IntentType 中: {r['name']}")
        if not r.get("utterances"):
            raise ValueError(f"route '{r['name']}' 的 utterances 为空")
        routes.append(Route(name=r["name"], utterances=r["utterances"],
                            score_threshold=r.get("score_threshold")))
    return routes


def save_thresholds(path: Path, routes: list[Route]) -> None:
    """把每个路由的 score_threshold 写回 routes.yaml（阈值调整的持久化端）。

    保留既有结构（routes 列表 + name/utterances）；score_threshold=None 时不输出
    该字段（保持文件最小变动，加载时回落到默认 None）。写回是阈值调整流程的一部分，
    不是运行时路径。
    """
    data = {"routes": []}
    for r in routes:
        entry = {"name": r.name, "utterances": r.utterances}
        if r.score_threshold is not None:
            entry["score_threshold"] = r.score_threshold
        data["routes"].append(entry)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def load_eval(path: Path = Path("data/intents/eval.yaml")) -> list[tuple[str, str, bool]]:
    """eval.yaml → [(query, intent_label, is_hard)]。

    独立评估样本集。is_hard 标记 query 为与其他意图近形的硬负样本：
    要求每个意图的 held-out 中硬负样本占比 ≥30%——否则 per-intent 阈值对
    混淆样本毫无约束力，是"自己给自己打分"的漏洞。
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    valid_names = {t.value for t in IntentType}
    items = []
    for e in data["eval"]:
        if e["intent"] not in valid_names:
            raise ValueError(f"eval 意图标签不在 IntentType 中: {e['intent']}")
        items.append((e["query"], e["intent"], bool(e.get("hard", False))))
    return items
