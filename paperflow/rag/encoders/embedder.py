"""稠密向量编码器：统一接口 + 真实的 bge 模型实现 + 测试用的确定性假实现。

真实的 bge 模型同时也被意图识别模块复用为它的向量编码实现。
"""
import hashlib
from pathlib import Path
from typing import Protocol

import numpy as np

# 模块级占位符：真实模型类在首次使用时才惰性导入并回填到这里。
# 必须保留为模块属性而不能只在函数内局部 import，因为测试会通过
# monkeypatch.setattr(embedder, "SentenceTransformer", stub) 把这里替换成
# 假模型，避免测试时联网下载真实权重。若改成函数内局部 import，
# monkeypatch 会因为模块上没有这个属性而报错，也绕过不了真实加载。
SentenceTransformer = None  # type: ignore[assignment]


class Embedder(Protocol):
    """编码器的统一接口：暴露向量维度 dim，并把一批文本编码成向量矩阵。

    dim 用于向量库确定集合的向量维度；__call__ 返回的行数与传入文本数一致。
    """
    @property
    def dim(self) -> int: ...

    def __call__(self, texts: list[str]) -> np.ndarray: ...


def _deterministic_seed(text: str) -> int:
    """确定性哈希种子——不能用内置 hash()（PYTHONHASHSEED 随机化跨进程不稳定）。"""
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16)


class FakeEmbedder:
    """测试用的假编码器：基于文本的 md5 生成确定性伪向量，维度可任意指定。

    同一文本在任何环境、任何进程下都会得到相同的向量，方便测试断言。
    ``calls`` 累计已编码的文本条数，供索引测试验证"内容未变的文档不会被
    重复编码"（统计数量而非调用次数，直接反映实际工作量）。
    """

    def __init__(self, dim: int = 64):
        """构造假编码器：指定伪向量维度，并把已编码文本数清零。"""
        self.dim = dim
        self.calls = 0

    def __call__(self, texts: list[str]) -> np.ndarray:
        """把一批文本编码成 L2 归一化的伪向量矩阵（每行一个文本）。"""
        self.calls += len(texts)
        vecs = []
        for t in texts:
            rng = np.random.RandomState(_deterministic_seed(t))
            v = rng.rand(self.dim)
            vecs.append(v / np.linalg.norm(v))   # L2 归一化，保证可用余弦相似度比较
        return np.array(vecs)


def resolve_model_dir(workspace: str, model_name: str) -> str:
    """把模型名解析成实际加载路径：优先用项目本地副本，其次才用官方模型名。

    模型文件很大（约 100MB）且不进版本库。把模型下载到工作区下的 models
    目录，可避免依赖全局模型缓存或外部目录路径；全新环境下本地没有模型时，
    改用官方模型名（首次使用时由依赖库自动下载）。

    解析顺序：
    ① model_name 本身就是一个已存在的本地目录 → 直接使用；
    ② 工作区 models 目录下存在同名子目录 → 使用本地副本；
    ③ 以上都没有 → 返回官方模型名。
    """
    if Path(model_name).is_dir():
        return model_name
    local = Path(workspace) / "models" / Path(model_name).name
    return str(local) if local.is_dir() else model_name


class BgeEmbedder:
    """真实的 bge 嵌入模型（基于 sentence-transformers），首次使用时才加载，CPU 推理。

    向量维度不写死，而是加载后从模型读取：不同 bge 型号维度不同
    （如 bge-small-zh-v1.5 是 512），硬编码容易出错。
    """

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        """记下模型名并预留惰性加载槽位（模型首次使用才真正加载）。"""
        self._model_name = model_name
        self._model = None
        self._dim: int | None = None

    def _load(self) -> None:
        """首次使用才加载模型：惰性导入权重、临时关掉加载进度条、读取向量维度。

        向量维度从模型读取而非硬编码（不同 bge 型号维度不同），新老版本
        sentence-transformers 的方法名不同，这里兼容两者。
        """
        # 惰性导入：sentence-transformers 导入耗时数秒，首次使用才加载。
        # global + 模块级占位符：把类名解析交给模块属性，测试的 monkeypatch
        # 替换即生效；真实环境首次走到这里才 import 并回填缓存。
        global SentenceTransformer
        if SentenceTransformer is None:
            from sentence_transformers import SentenceTransformer
        # 抑制权重加载进度条（tqdm "Loading weights"）——命令行启动不该刷屏。
        # tqdm 4.70 的 disable 是实例参数而非类属性，故临时改 __init__ 默认值：
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
        # 新版 sentence-transformers 把获取维度的方法改名了（旧名会告警）；
        # 新名优先，没有时改用旧名，兼容两种版本。
        get_dim = getattr(self._model, "get_embedding_dimension", None)
        if get_dim is None:
            get_dim = self._model.get_sentence_embedding_dimension
        self._dim = get_dim()

    @property
    def dim(self) -> int:
        """模型输出的向量维度（首次访问会触发模型加载）。"""
        if self._model is None:
            self._load()
        assert self._dim is not None
        return self._dim

    def __call__(self, texts: list[str]) -> np.ndarray:
        """把一批文本编码成向量矩阵（每行一个文本），输出已做 L2 归一化。

        归一化后的向量可直接用余弦相似度比较。
        """
        if self._model is None:
            self._load()
        # 清洗文本中未配对的代理字符（surrogate）。PDF 或外部文本常带这类
        # 非法字符，不清洗会让 tokenizer 抛 TypeError，导致整个检索流程
        # 不可用。清洗函数定义在安全模块里，这里只做调用。
        from paperflow.core.security.text import sanitize_surrogates
        texts = [sanitize_surrogates(t) for t in texts]
        return self._model.encode(texts, normalize_embeddings=True)
