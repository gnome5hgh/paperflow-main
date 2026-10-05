# paperflow/core/intent/routing/route_loader.py
"""意图知识库加载器——routes.yaml 是唯一知识库源（测试与生产共用路径）。"""
import warnings
from pathlib import Path

import yaml

from paperflow.core.intent.schemas.route import Route
from paperflow.core.intent.schemas.intent import IntentType


#: 2026-10-01 枚举收敛（spec 2026-10-01-intent-taxonomy-and-steps-design §3）过渡期
#: 已移除值：switch_topic 并入 set_research_topic、refine_query 并入 search_paper。
#: 数据 yaml（routes/eval）的重标迁移属 Task 3/4——在此之前加载侧对这两个旧值
#: **过滤并告警**而非报错：过滤掉的旧路由不可能再被路由器选中，其 query 落到近邻
#: 意图或 LLM 兜底（正是合并后的预期行为）；若照旧放行，pipeline 的
#: IntentType(choice.name) 会在旧路由胜出时崩溃。Task 3/4 重标落地后手动删除
#: 此表（显式 set 不会自己清空），过滤分支随之成为死防御（保留，防历史备份/
#: 分支数据回流）。
_REMOVED_VALUES = {"switch_topic", "refine_query"}


#: 仓库安装根（本文件位于 paperflow/core/intent/routing/，向上四级即仓库根）。
#: 随仓库发布的知识资产恒锚此处，不随 PAPERFLOW_RUNTIME_WORKSPACE 重定向。
_INSTALL_ROOT = Path(__file__).resolve().parents[4]

#: 仓库安装根下的 routes.yaml（默认路径不可用时回退：从非仓库目录启动、
#: 或 cwd 相对路径不存在时——routes.yaml 是随仓库发布的知识资产，恒锚仓库根，
#: 不随 PAPERFLOW_RUNTIME_WORKSPACE 重定向）
_INSTALL_ROOT_ROUTES = _INSTALL_ROOT / "data" / "intent" / "routes.yaml"

#: 路由向量缓存（HybridRouter 的 vector_cache_path）。语料源自安装根下的
#: data/intent，故与 routes.yaml 同锚安装根——不随 workspace 重定向；
#: 命中即零网络启动，未命中现算回写。
VECTOR_CACHE_PATH = _INSTALL_ROOT / "data" / "intent" / "routes_vectors.npz"


def load_routes(path: Path | None = None) -> list[Route]:
    """yaml → [Route(name, utterances, score_threshold)]。只读加载。

    路径解析：显式传入用之；否则 cwd 相对 data/intent/routes.yaml 优先
    （历史行为，仓库内启动零变化），不存在则回退仓库安装根副本。

    校验：① route 名必须在 IntentType 枚举中——否则 pipeline 的
    IntentType(choice.name) 会抛 ValueError 崩溃（routes.yaml 拼错/未同步枚举）
    ② utterances 非空——空列表 route 导致 fit([])（avg_doc_len 对空数组报错/NaN）

    Args:
        path: routes.yaml 文件路径，默认为 data/intent/routes.yaml。

    Returns:
        Route 对象列表。

    Raises:
        ValueError: 当 route 名不在 IntentType 中（已移除的收敛旧值除外——过滤告警，
            见 _REMOVED_VALUES），或 utterances 为空时。
    """
    if path is None:
        path = (Path("data/intent/routes.yaml") if Path("data/intent/routes.yaml").is_file()
                else _INSTALL_ROOT_ROUTES)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    valid_names = {t.value for t in IntentType}
    routes = []
    for r in data["routes"]:
        # 校验1：路由名必须存在于 IntentType 枚举中
        if r["name"] not in valid_names:
            if r["name"] in _REMOVED_VALUES:
                warnings.warn(
                    f"route '{r['name']}' 已于 2026-10-01 枚举收敛中移除"
                    f"（数据重标属 Task 3/4），本次加载过滤该路由",
                    stacklevel=2)
                continue
            raise ValueError(f"route 名不在 IntentType 中: {r['name']}")

        # 校验2：每个路由至少有一个示例句，否则训练时 BM25 会因空语料崩溃
        if not r.get("utterances"):
            raise ValueError(f"route '{r['name']}' 的 utterances 为空")
        routes.append(Route(name=r["name"], utterances=r["utterances"],
                            score_threshold=r.get("score_threshold") # 可选字段，缺失则为 None
                            )
                      )
    return routes


def save_thresholds(path: Path, routes: list[Route]) -> None:
    """把每个路由的 score_threshold 写回 routes.yaml（阈值调整的持久化端）。

    保留既有结构（routes 列表 + name/utterances）；score_threshold=None 时不输出
    该字段（保持文件最小变动，加载时回落到默认 None）。写回是阈值调整流程的一部分，
    不是运行时路径。

    Args:
        path: 要写入的 YAML 文件路径。
        routes: Route 对象列表（包含更新后的 score_threshold）。
    """
    data = {"routes": []}
    for r in routes:
        entry = {"name": r.name, "utterances": r.utterances}
        # 仅当阈值非 None 时才写入，保持文件整洁且加载时与默认行为一致
        if r.score_threshold is not None:
            entry["score_threshold"] = r.score_threshold
        data["routes"].append(entry)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def load_eval(path: Path) -> list[tuple[str, str, bool]]:
    """eval.yaml → [(query, intent_label, is_hard)]。

    独立评估样本集。is_hard 标记 query 为与其他意图近形的硬负样本：
    约束要求每个新增意图的 held-out 中硬负样本占比 ≥30%（既有意图可低于
    该值，整体占比约 32%）——否则 per-intent 阈值对混淆样本毫无约束力，
    是"自己给自己打分"的漏洞。

    Args:
        path: 题集文件路径（**必填**）。题集是实验资产，归各自实验目录所有
            （scripts/intent/calibration/goldens/{source,audited}/，将来的
            scripts/intent/eval/goldens/ 等），不放在随仓库发布的
            data/intent/——那里只留生产知识库 routes.yaml。故本函数不设默认路径，
            调用方显式传入。

    Returns:
        列表，每个元素为三元组 (query文本, 意图标签, 是否为硬负样本布尔值)。

    Raises:
        ValueError: 当 eval 中的意图标签不在 IntentType 中（已移除的收敛旧值除外——
            过滤告警，见 _REMOVED_VALUES）时。
    """
    if path is None:
        path = (Path("data/intent/routes.yaml") if Path("data/intent/routes.yaml").is_file()
                else _INSTALL_ROOT_ROUTES)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    valid_names = {t.value for t in IntentType}
    items = []
    for e in data["eval"]:
        # 校验标签合法性
        if e["intent"] not in valid_names:
            if e["intent"] in _REMOVED_VALUES:
                warnings.warn(
                    f"eval 意图 '{e['intent']}' 已于 2026-10-01 枚举收敛中移除"
                    f"（数据重标属 Task 3/4），本次加载过滤该样本",
                    stacklevel=2)
                continue
            raise ValueError(f"eval 意图标签不在 IntentType 中: {e['intent']}")
        # hard 字段默认为 False（若未提供）
        items.append((e["query"], e["intent"], bool(e.get("hard", False))))
    return items
