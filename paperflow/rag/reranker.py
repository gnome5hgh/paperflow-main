"""重排器：Cross-encoder 精排。真实 bge-reranker 惰性加载，测试用 FakeReranker。"""
import hashlib
from typing import Protocol

# 模块级占位：真实类在 _load() 里首次使用时才惰性导入并回填此名字。
# 之所以保留这个模块属性（而不是只在函数内局部 import），是因为测试需要用
# monkeypatch.setattr(reranker, "CrossEncoder", stub) 替换为假模型，
# 否则 CI 会下载真实权重。函数内局部 import 不会产生模块属性，setattr 会
# 因属性不存在而 AttributeError，且局部 import 也会绕过 monkeypatch。
CrossEncoder = None  # type: ignore[assignment]


class Reranker(Protocol):
    """重排协议：输入 query + docs，返回按相关度降序的文档索引（前 top_k 个）。"""

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]: ...


class FakeReranker:
    """测试替身：按 md5 稳定排序，确定性且与真实模型无关。"""

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        # 用 md5 对文档原文排序：同一语料在任何环境/进程下重排结果一致，
        # 便于测试断言确定性与 top_k 截断行为（不依赖真实模型或随机性）。
        order = sorted(range(len(docs)),
                       key=lambda i: hashlib.md5(docs[i].encode()).hexdigest())
        return order[:top_k]


class BgeReranker:
    """bge-reranker-v2-m3 Cross-encoder，惰性加载，CPU 推理。"""

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
        if self._model is None:
            self._load()
        # bge-reranker 输入是 [query, doc] 对，输出每对的相关性分数
        pairs = [[query, d] for d in docs]
        scores = self._model.predict(pairs)
        order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
        return order[:top_k]
