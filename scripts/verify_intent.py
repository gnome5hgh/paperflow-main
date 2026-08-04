# scripts/verify_intent.py
"""意图路由标定 + 门槛验证（真实 bge，验收后手动——真实模型不进 CI，spec §4.7.7）。

用法：conda run -n paperflow python scripts/verify_intent.py

流程（spec §4.7.4 标定闭环）：
  1. 加载 BgeEmbedder + load_routes（训练集）+ load_eval（held-out）
  2. fit() 校准 per-route 阈值（routes 为训练集；fit 已预计算得分，不逐候选重编码）
  3. 门槛断言（统一预测源 _pred 运行时路径，⚪3）：整体≥0.90 / 每意图≥0.80 /
     非general落general≤0.15
  4. 对照基线：FixedDenseEncoder **同样 fit**（🟠1——未 fit 时永不产生 general，
     对比失真；同 routes 同标定才公平），bge 必须 ≥ 基线
  5. 达标 → save_thresholds 写回 routes.yaml（交付态）；不达标 → 打印掉线意图，不写回

退出码：0=达标；1=未达标（可被 CI 化调用方消费）。
"""
import sys
from collections import defaultdict
from pathlib import Path

from paperflow.core.intent.hybrid_router import HybridRouter
from paperflow.core.intent.dense_encoder import FixedDenseEncoder
from paperflow.core.intent.route_loader import load_routes, load_eval, save_thresholds
from paperflow.rag.embedder import BgeEmbedder

OVERALL = 0.90        # 整体准确率门槛（spec §4.7.2）
PER_INTENT = 0.80     # 每意图准确率门槛
GENERAL_LEAK = 0.15   # 非 general 落 general 比率上限


def _intent_accuracy(preds: list[str], labels: list[str]) -> dict[str, float]:
    """按意图标签分组的准确率（分母=该意图标签的 eval 样本数）。"""
    per = defaultdict(lambda: [0, 0])
    for p, l in zip(preds, labels):
        per[l][1] += 1
        per[l][0] += int(p == l)
    return {k: (v[0] / v[1] if v[1] else 0.0) for k, v in per.items()}


def _route_accuracy(router, queries: list[str], labels: list[str]) -> tuple[float, dict[str, float], float]:
    """统一预测源（⚪3）：用运行时路径 `_pred`（逐 query __call__）计算全部门槛指标。

    不从 router.evaluate() 取 acc——evaluate() 走 _vec_evaluate（simulate_static 向量化）
    路径，与 _pred（生产实际走的运行时路径）在阈值边界可能分歧；门槛必须在**同一个**
    预测集上判定，且用生产路径。返回 (整体准确率, per-意图准确率, 非general落general比率)。
    """
    preds = [_pred(router, q) for q in queries]
    acc = sum(1 for p, l in zip(preds, labels) if p == l) / len(labels)
    per = _intent_accuracy(preds, labels)
    non_general = [l for l in labels if l != "general"]
    leaked = sum(1 for p, l in zip(preds, labels)
                 if l != "general" and p == "general")
    leak = leaked / len(non_general) if non_general else 0.0
    return acc, per, leak


def main() -> int:
    eval_items = load_eval()
    labels = [l for _, l in eval_items]
    queries = [q for q, _ in eval_items]
    # 训练集 = routes 全部例句（基线与 bge 共用同一份，apples-to-apples）
    routes = load_routes()
    train_x = [u for r in routes for u in r.utterances]
    train_y = [r.name for r in routes for _ in r.utterances]

    # 基线：FixedDenseEncoder，**同样 fit**（🟠1——未 fit 时 score_threshold=None →
    # hybrid_router.py:123 passed=True，路由器永不产生 general，bge≥基线对比平凡为真、
    # 毫无意义；同 routes 同标定流程才是"换 bge 是否真的更好"的公平测试）
    baseline = HybridRouter(encoder=FixedDenseEncoder(dim=64), routes=routes)
    baseline.fit(train_x, train_y)
    base_acc, _, _ = _route_accuracy(baseline, queries, labels)
    print(f"[基线] FixedDenseEncoder 整体准确率: {base_acc:.3f}")

    # ① 真实 bge + 标定
    router = HybridRouter(encoder=BgeEmbedder(), routes=routes)
    router.fit(train_x, train_y)
    print(f"[标定] fit 完成，per-route 阈值: {router.get_thresholds()}")

    # ② 门槛验证（单一预测源 _route_accuracy，⚪3）
    acc, per, leak_ratio = _route_accuracy(router, queries, labels)

    print(f"[门槛] 整体 {acc:.3f} (需 ≥{OVERALL}) | 每意图 {dict(per)} (需 ≥{PER_INTENT}) "
          f"| general 泄漏 {leak_ratio:.3f} (需 ≤{GENERAL_LEAK}) | bge≥基线: {acc >= base_acc}")

    ok = (acc >= OVERALL and all(v >= PER_INTENT for v in per.values())
          and leak_ratio <= GENERAL_LEAK and acc >= base_acc)
    if not ok:
        weak = [k for k, v in per.items() if v < PER_INTENT]
        print(f"[未达标] 掉线意图: {weak}。补 routes 重标定（spec §4.7.4），不写回阈值。")
        return 1

    # ③ 达标 → 写回交付态
    save_thresholds(Path("data/intents/routes.yaml"), router.routes)
    print("[达标] 阈值已写回 data/intents/routes.yaml（启动只 load，零 fit）")
    return 0


def _pred(router, query: str) -> str:
    """路由判定：命中返回 route 名，未命中返回 general（与 pipeline 语义一致）。"""
    choice = router(query)
    return choice.name if choice is not None else "general"


if __name__ == "__main__":
    sys.exit(main())
