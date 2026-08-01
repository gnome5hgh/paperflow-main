"""稠密编码器：协议 + 真实 bge + 测试替身。真实 bge 同时是 Intent DenseEncoder 的替换实现。"""
import hashlib
from typing import Protocol

import numpy as np

# 模块级占位：真实类在 _load() 里首次使用时才惰性导入并回填此名字。
# 之所以保留这个模块属性（而不是只在函数内局部 import），是因为测试需要用
# monkeypatch.setattr(embedder, "SentenceTransformer", stub) 替换为假模型，
# 否则 CI 会下载真实权重。函数内局部 import 不会产生模块属性，setattr 会
# 因属性不存在而 AttributeError，且局部 import 也会绕过 monkeypatch。
SentenceTransformer = None  # type: ignore[assignment]


class Embedder(Protocol):
    """语义对齐 core/intent 的 DenseEncoder 协议；dim 供向量库建集合用。"""
    @property
    def dim(self) -> int: ...

    def __call__(self, texts: list[str]) -> np.ndarray: ...


def _deterministic_seed(text: str) -> int:
    """确定性哈希种子——不能用内置 hash()（PYTHONHASHSEED 随机化跨进程不稳定）。"""
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16)


class FakeEmbedder:
    """测试替身：md5 确定性伪向量（对齐 FixedDenseEncoder 模式），维度任意。"""

    def __init__(self, dim: int = 64):
        self.dim = dim

    def __call__(self, texts: list[str]) -> np.ndarray:
        vecs = []
        for t in texts:
            rng = np.random.RandomState(_deterministic_seed(t))
            v = rng.rand(self.dim)
            vecs.append(v / np.linalg.norm(v))   # L2 归一化（对齐余弦语义）
        return np.array(vecs)


class BgeEmbedder:
    """真实 bge（sentence-transformers），惰性单例加载，CPU 推理。

    维度不硬编码——从模型 get_sentence_embedding_dimension() 读取
    （bge-small-zh-v1.5 实际 512，勿写死 384）。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        self._model_name = model_name
        self._model = None
        self._dim: int | None = None

    def _load(self) -> None:
        # 惰性导入：sentence-transformers 导入耗时数秒，首次使用才加载。
        # global + 模块级占位符：把类名解析交给模块属性，测试的 monkeypatch
        # 替换即生效；真实环境首次走到这里才 import 并回填缓存。
        global SentenceTransformer
        if SentenceTransformer is None:
            from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self._model_name)
        self._dim = self._model.get_sentence_embedding_dimension()

    @property
    def dim(self) -> int:
        if self._model is None:
            self._load()
        assert self._dim is not None
        return self._dim

    def __call__(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            self._load()
        return self._model.encode(texts, normalize_embeddings=True)
