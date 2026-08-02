# scripts/verify_rag.py
"""真实模型 smoke：验证 bge embedder/reranker + GROBID 解析 + 检索链路。

用法（验收后手动执行，需已装依赖 + GROBID Docker）：
    conda run -n paperflow python scripts/verify_rag.py
首次运行会下载模型权重（~30MB + ~300MB）。输出分四段验证。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # scripts/ 不在包内

from paperflow.config import PaperFlowConfig
from paperflow.rag.service import get_rag_service


def main() -> int:
    config = PaperFlowConfig.from_env()
    svc = get_rag_service(config)

    print("== 1. GROBID 探测 ==")
    print("GROBID 可用:", svc.grobid_available())

    print("== 2. 真实 embedder ==")
    vecs = svc._ensure_embedder()(["circRNA 调控 miRNA 表达", "drug target prediction"])
    print("向量形状:", vecs.shape, "（bge-small-zh-v1.5 应为 512 维）")

    print("== 3. 真实 reranker ==")
    order = svc._ensure_reranker()("circRNA 机制", ["circRNA paper", "unrelated text"], 1)
    print("重排索引:", order)

    print("== 4. 检索链路（索引需先有数据）==")
    svc.get_indexer().index_all()
    hits = svc.retrieve("circRNA 网络", top_k=3)
    print("检索命中:", len(hits))
    for h in hits:
        print(" -", h.path, ":", h.text[:80].replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
