"""稠密向量编码器：统一的编码契约 + 真实的 Qwen3 嵌入模型实现。

真实的千问嵌入模型同时也被意图识别模块复用为它的向量编码实现；
测试用的确定性假编码器（FixedDenseEncoder / FakeEmbedder）已迁至 tests/conftest.py。
"""
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

    Protocol 是 Python 3.8 在 typing 模块中引入的一种结构化子类型（structural subtyping）机制，本质上是一种行为契约。
    它和 Java 的接口类似，但更灵活——不要求显式继承。只要一个类实现了 Protocol 中定义的所有方法和属性，类型检查器（如 mypy）就会认为它"符合"这个协议。
    任何类，只要满足以下两个条件，就自动被视为符合 Embedder 协议：
    - 有 dim 属性（返回 int）
    - 可以被调用（__call__），接收 list[str]，返回 np.ndarray

    这是全仓库唯一的稠密编码契约（原意图侧 dense.py 的 DenseEncoder 协议已并入这里）：
    RAG 向量库消费 `dim` 建集合，意图混合路由器只调用 `__call__` 做余弦相似度。
    """
    @property
    def dim(self) -> int: ...

    def __call__(self, texts: list[str]) -> np.ndarray: ...


def resolve_model_dir(workspace: str, model_name: str) -> str:
    """把模型名解析成实际加载路径：优先用项目本地副本，其次才用官方模型名。

    模型文件很大（约 100MB）且不进版本库。把模型下载到工作区下的 models
    目录，可避免依赖全局模型缓存或外部目录路径；全新环境下本地没有模型时，
    改用官方模型名（首次使用时由依赖库自动下载）。

    解析顺序：
    ① model_name 本身就是一个已存在的本地目录 → 直接使用；
    ② 工作区 models 目录下存在同名子目录 → 使用本地副本；
    ③ 以上都没有 → 返回官方模型名。

    Args:
        workspace: 工作区根目录路径。
        model_name: 模型名，如 "Qwen/Qwen3-Embedding-0.6B" 或本地路径。

    Returns:
        str: 解析后的模型加载路径。
    """
    if Path(model_name).is_dir():
        return model_name
    local = Path(workspace) / "models" / Path(model_name).name
    return str(local) if local.is_dir() else model_name


class SbertEmbedder:
    """真实的千问嵌入模型（基于 sentence-transformers），首次使用时才加载，CPU 推理。

    向量维度不写死，而是加载后从模型读取：不同型号维度不同
    （如 Qwen3-Embedding-0.6B 是 1024），硬编码容易出错。

    千问嵌入模型是指令感知模型（instruction aware）：官方建议检索场景下
    只给 query 侧附加 task instruction（可再提升 1–5%）。当前 Embedder 协议
    的 `__call__(texts)` 不区分 query 与 doc，调用方（索引器/路由器/检索器）
    均不加 instruction，属可接受的简化；后续若需榨取精度，可扩展协议加
    可选 instruction 参数。
    """

    def __init__(self, model_name: str = "Qwen/Qwen3-Embedding-0.6B"):
        """初始化千问嵌入器，此时不加载模型。
        记下模型名并预留惰性加载槽位（模型首次使用才真正加载）。

        Args:
            model_name: 模型名称或路径，支持本地目录或 HuggingFace 模型 ID。
        """
        self._model_name = model_name
        self._model = None           # 真实模型实例，首次调用时加载
        self._dim: int | None = None # 向量维度，加载后填充

    def _load(self) -> None:
        """首次使用才加载模型：惰性导入权重、临时关掉加载进度条、读取向量维度。

        向量维度从模型读取而非硬编码（不同型号维度不同），新老版本
        sentence-transformers 的方法名不同，这里兼容两者。
        """
        # 使用模块级 `SentenceTransformer` 占位符实现真实类的惰性导入，支持测试时用 monkeypatch 替换为假实现。
        # 真实环境首次走到这里才 import 并回填缓存。
        global SentenceTransformer
        if SentenceTransformer is None:
            from sentence_transformers import SentenceTransformer

        # ---- 临时禁用 tqdm 进度条 ----
        import tqdm as _tqdm_mod
        _orig_init = _tqdm_mod.tqdm.__init__

        def _quiet_init(self, *args, **kwargs):
            kwargs.setdefault("disable", True)
            _orig_init(self, *args, **kwargs)

        _tqdm_mod.tqdm.__init__ = _quiet_init
        try:
            self._model = SentenceTransformer(self._model_name)
        finally:
            # 确保无论加载是否成功，都恢复 tqdm 原始行为
            _tqdm_mod.tqdm.__init__ = _orig_init

        # ---- 读取向量维度（兼容新旧 API） ----
        # 新版 sentence-transformers 把获取维度的方法改名了（旧名会告警）；新名优先，没有时改用旧名，兼容两种版本。
        get_dim = getattr(self._model, "get_embedding_dimension", None)
        if get_dim is None:
            get_dim = self._model.get_sentence_embedding_dimension
        self._dim = get_dim()

    @property
    def dim(self) -> int:
        """模型输出的向量维度（首次访问会触发模型加载）。

        Returns:
            int: 向量维度。

        Raises:
            AssertionError: 如果模型加载后 _dim 仍为 None（防御性检查）。
        """
        if self._model is None:
            self._load()
        assert self._dim is not None
        return self._dim

    def __call__(self, texts: list[str]) -> np.ndarray:
        """把一批文本编码成向量矩阵（每行一个文本），输出已做 L2 归一化。归一化后的向量可直接用余弦相似度比较。

        Args:
            texts: 待编码的文本列表。

        Returns:
            np.ndarray: 形状为 (len(texts), dim) 的归一化向量矩阵。
        """
        # 1. 若模型未加载，触发 `_load()`。
        if self._model is None:
            self._load()

        # 2. 清洗输入文本中的非法代理字符（surrogate）。PDF 解析或外部输入
        #    常包含未配对的代理字符（如 `\ud800`），若不清理，sentence-transformers
        #    的 tokenizer 会抛出 `TypeError`，导致整个检索流程崩溃。
        from paperflow.core.security.text import sanitize_surrogates
        texts = [sanitize_surrogates(t) for t in texts]

        # 3. 调用模型编码，`normalize_embeddings=True` 执行 L2 归一化，确保输出向量模长为 1，可直接用于余弦相似度计算。
        return self._model.encode(texts, normalize_embeddings=True)
