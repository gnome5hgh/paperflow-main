"""稠密编码器：协议 + 真实 bge + 测试替身。真实 bge 同时是 Intent DenseEncoder 的替换实现。"""
import hashlib
from pathlib import Path
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
    """测试替身：md5 确定性伪向量（对齐 FixedDenseEncoder 模式），维度任意。

    ``calls`` 累计已 embedding 的文本数，供 indexer 测试断言
    guard-2 不重 embedding 不变文档（数量而非次数，直接反映工作量）。"""

    def __init__(self, dim: int = 64):
        self.dim = dim
        self.calls = 0

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.calls += len(texts)
        vecs = []
        for t in texts:
            rng = np.random.RandomState(_deterministic_seed(t))
            v = rng.rand(self.dim)
            vecs.append(v / np.linalg.norm(v))   # L2 归一化（对齐余弦语义）
        return np.array(vecs)


def resolve_model_dir(workspace: str, model_name: str) -> str:
    """HF 模型名 → 实际加载路径（项目本地优先，回退 HF 名）。

    模型是运行时大文件（~100MB，`data/*` gitignored 不进 git）——项目本地化
    （`<workspace>/models/<name>/`）避免依赖全局 HF 缓存或外部项目路径；fresh
    clone 无本地模型时回退 HF 名（首次使用自动下载）。

    解析顺序：① model_name 本身已是存在的本地目录 → 直接用；②
    `<workspace>/models/<model_name 末段>` 存在 → 用本地；③ 否则回退 HF 名。
    """
    if Path(model_name).is_dir():
        return model_name
    local = Path(workspace) / "models" / Path(model_name).name
    return str(local) if local.is_dir() else model_name


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
        # 抑制权重加载进度条（tqdm "Loading weights"）——CLI 启动不该刷屏。
        # tqdm 4.70 的 disable 是实例参数非类属性，故补丁 __init__ 默认值：
        # 仅在本次加载期间生效，加载完恢复（不污染后续正常进度显示）。
        import tqdm as _tqdm_mod
        _orig_init = _tqdm_mod.tqdm.__init__

        def _quiet_init(self, *args, **kwargs):
            kwargs.setdefault("disable", True)
            _orig_init(self, *args, **kwargs)

        _tqdm_mod.tqdm.__init__ = _quiet_init
        try:
            self._model = SentenceTransformer(self._model_name)
        finally:
            _tqdm_mod.tqdm.__init__ = _orig_init
        # sentence-transformers 5.x 把 get_sentence_embedding_dimension 重命名为
        # get_embedding_dimension（FutureWarning）；新名优先，旧名回退兼容两种版本。
        get_dim = getattr(self._model, "get_embedding_dimension", None)
        if get_dim is None:
            get_dim = self._model.get_sentence_embedding_dimension
        self._dim = get_dim()

    @property
    def dim(self) -> int:
        if self._model is None:
            self._load()
        assert self._dim is not None
        return self._dim

    def __call__(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            self._load()
        # 清洗未配对 surrogate（PDF/外部文本可能携带）——否则 tokenizer 抛
        # TextEncodeInput TypeError，意图路由整条降级（见 core/text_util.py）。
        from paperflow.core.text_util import sanitize_surrogates
        texts = [sanitize_surrogates(t) for t in texts]
        return self._model.encode(texts, normalize_embeddings=True)
