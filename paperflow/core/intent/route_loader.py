# paperflow/core/intent/route_loader.py
"""意图知识库加载器——routes.yaml 是唯一知识库源（测试与生产共用路径）。"""
from pathlib import Path

import yaml

from paperflow.core.intent.schema import Route
from paperflow.core.intent.intent_schema import IntentType


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
