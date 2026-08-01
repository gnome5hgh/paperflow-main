# paperflow/core/intent/dense_encoder.py
"""稠密编码器接口 + Layer 1 fake（真实 bge 留 Layer 2 RAG 落地时替换）。"""
import hashlib
from typing import Protocol

import numpy as np


class DenseEncoder(Protocol):
    """语义对齐 encoders/base.py 的 DenseEncoder 接口。
    Layer 2 落地时由真实 bge 实现替换——HybridRouter 只依赖此协议，零改动切换。"""

    def __call__(self, texts: list[str]) -> np.ndarray: ...


def _deterministic_seed(text: str) -> int:
    """确定性哈希种子——⚠️ 不能用内置 hash()：PYTHONHASHSEED 随机化导致
    同一文本跨进程向量不同，对照验证（ours/theirs 两个独立进程）直接失效。"""
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16)


class FixedDenseEncoder:
    """Layer 1 fake：确定性伪向量（md5 种子），仅用于路由逻辑验证。
    跨进程稳定：同一文本 → 同一向量（对照验证的前提）。
    确定性只来自 md5(text)——无实例级随机状态。"""

    def __init__(self, dim: int = 384):
        self.dim = dim

    def __call__(self, texts: list[str]) -> np.ndarray:
        return np.array([self._vec(t) for t in texts])

    def _vec(self, text: str) -> np.ndarray:
        rng = np.random.RandomState(_deterministic_seed(text))
        v = rng.rand(self.dim)
        return v / np.linalg.norm(v)      # 归一化（对齐真实余弦语义）
