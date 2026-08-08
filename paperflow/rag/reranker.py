"""重排器：用 Cross-encoder 模型对初检结果做精排。真实模型首次使用时才加载，测试用确定性假实现。"""
import hashlib
from typing import Protocol

# 模块级占位符：真实模型类在首次使用时才惰性导入并回填到这里。
# 必须保留为模块属性而不能只在函数内局部 import，因为测试会通过
# monkeypatch.setattr(reranker, "CrossEncoder", stub) 把这里替换成假模型，
# 避免测试时联网下载真实权重。若改成函数内局部 import，monkeypatch 会
# 因为模块上没有这个属性而报错，也绕过不了真实加载。
CrossEncoder = None  # type: ignore[assignment]


class Reranker(Protocol):
    """重排接口：输入 query 与候选文档列表，返回按相关度降序的文档下标（前 top_k 个）。"""

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]: ...


class FakeReranker:
    """测试用的假重排器：按文档原文的 md5 稳定排序，与真实模型无关。"""

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        """返回按 md5 排序后的前 top_k 个文档下标（结果确定、可复现）。"""
        # 用 md5 对文档原文排序：同一语料在任何环境/进程下重排结果一致，
        # 便于测试断言确定性与 top_k 截断行为（不依赖真实模型或随机性）。
        order = sorted(range(len(docs)),
                       key=lambda i: hashlib.md5(docs[i].encode()).hexdigest())
        return order[:top_k]


class BgeReranker:
    """bge-reranker-v2-m3 Cross-encoder 重排模型，惰性加载，CPU 推理。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3"):
        self._model_name = model_name
        self._model = None

    def _load(self) -> None:
        # 惰性导入：sentence-transformers 导入耗时数秒，首次使用才加载。
        # global + 模块级占位符：把类名解析交给模块属性，测试的 monkeypatch
        # 替换即生效；真实环境首次走到这里才 import 并回填缓存。
        global CrossEncoder
        if CrossEncoder is None:
            from sentence_transformers import CrossEncoder
        self._model = CrossEncoder(self._model_name)

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        """对每个候选文档给出与 query 的相关性分数，按分数降序返回前 top_k 个文档的下标。"""
        if self._model is None:
            self._load()
        # bge-reranker 的输入是 [query, doc] 对，输出每对的相关性分数
        pairs = [[query, d] for d in docs]
        scores = self._model.predict(pairs)
        order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
        return order[:top_k]
