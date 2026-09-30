"""RAG 检索评测：黄金集 + hit_rate@k / MRR 纯脚本计算（无 LLM judge）。

黄金集文件为 JSONL，每行一个 JSON 对象：
    {"query": "……", "expected": [{"path": "note/xx.md", "heading": "3 批判性评估"}]}
- path 与 Chunk.path 同一坐标系（相对知识库根）。命中判定按 path 级：任一期望
  path 出现在检索结果 top-k 内即算命中。
- heading 是可选的严格判定参考：path 命中且期望 heading 出现在该块文本首行
  （索引侧拼的「标题 > 章节」前缀行）才算 strict 命中——切块升级前无前缀行，
  strict 指标自然偏低，仅作改造前后对照。

用法（需 Milvus 与真实索引在跑）：
    python -m paperflow.rag.evaluation --golden data/rag/eval/rag_golden.jsonl \
        --out data/rag/eval/after.json --compare data/rag/eval/baseline.json
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

#: 评测算的 k 档位；一次 top-10 检索同时覆盖三档
_EVAL_KS = (3, 5, 10)


def load_golden(path: str) -> list[dict]:
    """读黄金集 JSONL，跳过格式非法的行并计数警告（评测不因单行脏数据中断）。

    Returns:
        list[dict]: 每项 {"query": str, "expected": [{"path": str, "heading": str|None}]}。
    """
    items: list[dict] = []
    skipped = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            query = raw["query"]
            expected = raw["expected"]
            if not (isinstance(query, str) and query.strip()):
                raise ValueError("query 非空字符串")
            if not (isinstance(expected, list) and expected):
                raise ValueError("expected 非空列表")
            norm = [{"path": str(e["path"]), "heading": e.get("heading")}
                    for e in expected]
            if any(not e["path"] for e in norm):
                raise ValueError("expected.path 非空")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            skipped += 1
            continue
        items.append({"query": query, "expected": norm})
    if skipped:
        print(f"⚠️ 黄金集有 {skipped} 行格式非法被跳过")
    return items


def evaluate(service, golden: list[dict], ks: tuple[int, ...] = _EVAL_KS) -> dict:
    """对黄金集逐条检索并计算指标。

    Args:
        service: RAGService 或提供 retrieve(query, top_k) 的对象（测试注入假实现）。
        golden: load_golden 的产出。
        ks: hit_rate 的 k 档位。

    Returns:
        dict: {"metrics": {指标名: 值}, "per_item": [{query, best_rank, strict}]}。
              golden 为空时各指标为 0、per_item 为空。
    """
    top_k = max(ks)
    metrics = {f"hit_rate@{k}": 0.0 for k in ks}
    metrics["mrr"] = 0.0
    metrics[f"strict_hit_rate@{top_k}"] = 0.0
    per_item: list[dict] = []
    if not golden:
        return {"metrics": metrics, "per_item": per_item}

    for item in golden:
        chunks = service.retrieve(item["query"], top_k=top_k)
        paths = [c.path for c in chunks]
        # path 级命中：任一期望 path 的最靠前名次（1-based；0 = 未命中）
        ranks = [paths.index(e["path"]) + 1 for e in item["expected"] if e["path"] in paths]
        best_rank = min(ranks) if ranks else 0
        # strict：命中块的首行（前缀行）包含期望 heading
        strict = False
        for e in item["expected"]:
            if e["path"] in paths and e.get("heading"):
                first_line = chunks[paths.index(e["path"])].text.split("\n", 1)[0]
                if e["heading"] in first_line:
                    strict = True
                    break
        for k in ks:
            if 0 < best_rank <= k:
                metrics[f"hit_rate@{k}"] += 1
        if best_rank:
            metrics["mrr"] += 1.0 / best_rank
        if strict:
            metrics[f"strict_hit_rate@{top_k}"] += 1
        per_item.append({"query": item["query"], "best_rank": best_rank, "strict": strict})

    n = len(golden)
    for key in metrics:
        metrics[key] = round(metrics[key] / n, 4)
    return {"metrics": metrics, "per_item": per_item}


def main() -> None:
    """CLI：跑评测、打印指标、可选存档（--out）与对比历史存档（--compare）。"""
    parser = argparse.ArgumentParser(description="RAG 黄金集评测（hit_rate@k / MRR）")
    parser.add_argument("--golden", required=True, help="黄金集 JSONL 路径")
    parser.add_argument("--out", default=None, help="结果存档 JSON 路径")
    parser.add_argument("--compare", default=None, help="历史存档 JSON，打印指标变化")
    args = parser.parse_args()

    from paperflow.rag.services.rag_service import get_rag_service

    golden = load_golden(args.golden)
    if not golden:
        print("黄金集为空，无法评测")
        return
    result = evaluate(get_rag_service(), golden)
    print(f"黄金集 {len(golden)} 条：")
    for key, val in result["metrics"].items():
        print(f"  {key}: {val}")

    if args.out:
        payload = {"timestamp": datetime.now().isoformat(timespec="seconds"),
                   "golden": args.golden, "golden_count": len(golden), **result}
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"已存档到 {args.out}")

    if args.compare:
        prev = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(f"\n与 {args.compare} 对比：")
        for key, val in result["metrics"].items():
            old = prev.get("metrics", {}).get(key)
            if old is not None:
                delta = round(val - old, 4)
                print(f"  {key}: {old} -> {val} ({'+' if delta >= 0 else ''}{delta})")


if __name__ == "__main__":
    main()
