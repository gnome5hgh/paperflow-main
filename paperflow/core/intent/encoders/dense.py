# paperflow/core/intent/encoders/dense.py
"""稠密编码器接口协议 + 确定性伪实现。

当前提供接口协议与一个用于路由逻辑验证的确定性伪编码器；
后续接入真实向量模型（如 bge）时，只需实现同一接口即可无缝替换。
"""
import hashlib
from typing import Protocol

import numpy as np


class DenseEncoder(Protocol):
    """稠密编码器接口协议。

    路由器只依赖这个协议；后续接入真实向量模型（如 bge）时，
    实现同一接口即可零改动切换。"""

    def __call__(self, texts: list[str]) -> np.ndarray: ...


def _deterministic_seed(text: str) -> int:
    """由文本生成确定性哈希种子。

    ⚠️ 不能用内置 hash()：PYTHONHASHSEED 随机化会导致同一文本在
    不同进程里得到不同向量，跨进程对比验证会直接失效。"""
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16)


class FixedDenseEncoder:
    """确定性伪向量编码器（md5 种子），仅用于路由逻辑验证。

    跨进程稳定：同一文本总是得到同一向量（这是对比验证的前提）。
    确定性只来自 md5(text)——没有任何实例级随机状态。"""

    def __init__(self, dim: int = 384):
        self.dim = dim

    def __call__(self, texts: list[str]) -> np.ndarray:
        return np.array([self._vec(t) for t in texts])

    def _vec(self, text: str) -> np.ndarray:
        rng = np.random.RandomState(_deterministic_seed(text))
        v = rng.rand(self.dim)
        return v / np.linalg.norm(v)      # 归一化（对齐真实余弦语义）
