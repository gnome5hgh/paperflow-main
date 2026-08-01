#!/usr/bin/env python
"""scripts/compare_router.py —— semantic-router 0.1.16 对照验证脚本（代码资产，不执行 theirs 侧）。

用法（两处各跑一次，输出对比）：
   1. paperFlow 侧:  python scripts/compare_router.py --impl ours
   2. 0.1.16 venv 侧: python compare_router.py --impl theirs
固定 X/y 数据集 → 对比 evaluate() accuracy 输出必须一致。

关键约束：
   ① 脚本自包含：Fixed encoders 定义在脚本内，theirs 侧不依赖 paperflow 包
   ② dense 和 sparse 都固定——仅固定 dense 时 ours(jieba)/theirs(bert)
     的稀疏向量天然不同，对照失去意义
   ③ 确定性种子：md5(text)（不能用内置 hash()——PYTHONHASHSEED 跨进程随机化）
   ④ ours 侧 import paperflow 需 sys.path.insert(0, 项目根)（scripts/ 不在包内）
   ⑤ --impl 分支延迟 import（ours 环境没装 semantic-router、theirs 没装 paperflow）
   ⑥ evaluate 两侧均返回单个 float（0.1.16 的 evaluate 返回 float，非 per-batch
      accuracy 列表）——直接 print，不需要 np.mean 包装
   ⑦ theirs 侧 Route 从 semantic_router.route import——0.1.16 的 schema.py 无 Route
      类（只有 EncoderType/RouteChoice/SparseEmbedding 等），Route 定义在 route.py
   ⑧ theirs 侧构造后需显式 router.add(routes)——0.1.16 的 HybridRouter.__init__
      只设置 encoder/索引对象，不调用 self.add(routes)，索引为空则 evaluate 恒为 0；
      ours 侧（paperflow 版）构造时已 add，无需重复
"""
import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # 约束 ④


def _deterministic_seed(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16)


class FixedDenseEncoder:
    """双方共用：相同文本 → 相同向量（确定性）。"""
    def __init__(self, dim: int = 384):
        self.dim = dim

    def __call__(self, texts: list[str]) -> np.ndarray:
        return np.array([self._vec(t) for t in texts])

    def _vec(self, text: str) -> np.ndarray:
        rng = np.random.RandomState(_deterministic_seed(text))
        v = rng.rand(self.dim)
        return v / np.linalg.norm(v)


class FixedSparseEncoder:
    """双方共用：相同文本 → 相同稀疏向量（确定性）。完全绕过 BM25 分词差异。
    两个 key 用独立空间（%97+1 与 100+%97+1）——避免同 key 覆盖（0.5 丢失）。

    fit / encode_documents：paperFlow 侧 HybridRouter.add() 按 BM25Encoder 接口
    （fit 建语料统计 + encode_documents 出文档稀疏向量）调用；theirs 侧
    add() 同样调用这两个方法。此处给 no-op fit + 与 __call__ 一致的
    encode_documents，保证两边的 router.add(routes) 都能跑通（fit 只做
    语料统计，固定向量不需要——返回 self 保持链式兼容）。"""
    def __call__(self, texts):
        return [{(_deterministic_seed(t) % 97) + 1: 0.5,
                 100 + (_deterministic_seed(t) % 97) + 1: 0.3}
                for t in texts]

    def encode_documents(self, texts):
        return self(texts)   # 文档与查询同构：确定性 dict，无需 TF/IDF 区分

    def fit(self, texts):
        return self          # no-op：固定向量不依赖语料统计


ROUTES = [
    ("search_paper", ["下载最新论文", "搜索 circRNA 文献"]),
    ("generate_note", ["把这篇论文整理成笔记", "写一份笔记"]),
    ("ask_question", ["circRNA 的机制是什么", "解释一下这个公式"]),
]

X = ["下载最新论文", "搜索 circRNA 文献", "把这篇论文整理成笔记", "写一份笔记",
     "circRNA 的机制是什么", "解释一下这个公式",
     "下载新论文", "整理笔记", "机制是什么"]
y = ["search_paper", "search_paper", "generate_note", "generate_note",
     "ask_question", "ask_question",
     "search_paper", "generate_note", "ask_question"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--impl", choices=["ours", "theirs"], required=True)
    args = parser.parse_args()

    if args.impl == "ours":
        from paperflow.core.intent.hybrid_router import HybridRouter   # 约束 ⑤
        from paperflow.core.intent.schema import Route as RouteCls
        sparse_encoder = FixedSparseEncoder()
    else:
        from semantic_router.routers.hybrid import HybridRouter        # 约束 ⑤
        from semantic_router.route import Route as RouteCls            # 约束 ⑦：0.1.16 的 Route 在 route 模块
        from semantic_router.schema import SparseEmbedding

        class SparseAdapter:
            """0.1.16 的 _convex_scaling 要求 SparseEmbedding.to_dict()——dict 直接传入必炸。"""
            def __call__(self, texts):
                return [SparseEmbedding.from_dict(d) for d in FixedSparseEncoder()(texts)]

        sparse_encoder = SparseAdapter()

    routes = [RouteCls(name=n, utterances=u) for n, u in ROUTES]   # 按分支的 Route 类

    router = HybridRouter(encoder=FixedDenseEncoder(), routes=routes,
                          sparse_encoder=sparse_encoder)
    if args.impl == "theirs":
        # 约束 ⑧：0.1.16 的 HybridRouter.__init__ 只设置 encoder/稀疏编码器/索引对象，
        # 不调用 self.add(routes)——不显式 add 则索引为空，evaluate 恒返回 0。
        # ours 侧（paperflow 版）构造时内部已 self.add(routes)，无需重复。
        router.add(routes)

    # 约束 ⑥：两侧 evaluate 均返回单个 float，直接输出，不需要 np.mean 包装
    acc = router.evaluate(X, y)
    print(f"[{args.impl}] accuracy = {acc:.4f}")


if __name__ == "__main__":
    main()
