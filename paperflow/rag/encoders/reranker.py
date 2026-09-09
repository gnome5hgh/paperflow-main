"""重排器：用 Cross-encoder 模型对初检结果做精排。真实模型首次使用时才加载。"""

from typing import Protocol

# ---- 模块级占位符（支持测试时用 monkeypatch 替换为假实现） ----
# CrossEncoder 类在首次使用时惰性导入并回填到模块属性。
# 不能改成函数内局部 import，否则测试时 monkeypatch.setattr(reranker, "CrossEncoder", stub)
# 会因模块上没有该属性而失败，也绕不过真实加载。
CrossEncoder = None  # type: ignore[assignment]


class Reranker(Protocol):
    """重排器接口协议。

    实现该协议的类需提供 __call__ 方法：
    输入查询文本和候选文档列表，返回按相关度降序排列的文档下标列表（前 top_k 个）。
    """

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        """对候选文档进行精排。

        Args:
            query: 查询文本。
            docs: 候选文档内容列表，顺序与原始候选列表一致。
            top_k: 需要返回的结果数量。

        Returns:
            list[int]: 按相关度从高到低排序的文档下标列表，长度不超过 top_k。
        """
        ...


class SbertReranker:
    """基于 Qwen3-Reranker-0.6B 的 Cross-encoder 重排模型。

    特性：
    - 惰性加载：模型在首次调用 __call__ 时才加载，避免启动时耗时。
    - CPU 推理：适合离线环境，不依赖 GPU。
    - Cross-encoder 架构：对 query 和每个 doc 进行联合编码，精排质量优于双编码器（bi-encoder）。
    - 千问重排模型底层是因果注意力 + last-token pooling 的生成式打分结构，
      由 sentence-transformers（>=5.4）的包装层负责拼接内部模板并输出相关性分数，
      调用方仍只需 `predict([[query, doc], ...])`。
    """

    def __init__(self, model_name: str = "Qwen/Qwen3-Reranker-0.6B"):
        """记下模型名并预留惰性加载槽位（模型首次使用才真正加载）。

        Args:
            model_name: 模型名称或本地路径，默认使用千问重排模型。
        """
        self._model_name = model_name
        self._model = None # 真实模型实例，首次调用时加载

    def _load(self) -> None:
        """首次使用才加载 Cross-encoder 模型（导入耗时数秒）。

        实现要点：
        - 使用模块级 `CrossEncoder` 占位符，支持测试替换。
        - 首次真实加载时，从 `sentence_transformers` 导入 `CrossEncoder` 类。
        - 加载后的模型实例保存在 `self._model` 中，后续调用复用。
        """
        # 惰性导入：sentence-transformers 导入耗时数秒，首次使用才加载。
        # global + 模块级占位符：把类名解析交给模块属性，测试的 monkeypatch
        # 替换即生效；真实环境首次走到这里才 import 并回填缓存。
        global CrossEncoder
        if CrossEncoder is None:
            from sentence_transformers import CrossEncoder
        # activation_fn="sigmoid"：千问重排模型的原始输出是 yes/no 两个 token 的
        # logit，包装层经 sigmoid 映射成 0–1 的相关性概率；分数越大越相关，
        # 单调性与原始分数一致，下游排序逻辑无需感知激活函数差异。
        self._model = CrossEncoder(self._model_name, activation_fn="sigmoid")

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        """对每个候选文档给出与 query 的相关性分数，按分数降序返回前 top_k 个文档的下标。

        边界条件：
        - 若 docs 为空列表，返回空列表（不会触发模型加载）。
        - top_k 大于 len(docs) 时，返回所有文档下标。
        - 模型输出分数是实数，排序稳定（但相同分数顺序不影响最终结果）。

        Args:
            query: 查询文本。
            docs: 候选文档内容列表，顺序与原始候选列表一致。
            top_k: 需要返回的结果数量。

        Returns:
            list[int]: 按相关度降序的文档下标列表，长度 = min(top_k, len(docs))。
        """
        # 1. 若模型未加载，触发 _load()。
        # 空候选列表直接返回，避免不必要的模型加载
        if self._model is None:
            self._load()

        # 2. 构造输入对：`[[query, doc1], [query, doc2], ...]`，批量推理
        # 重排模型的输入是 [query, doc] 对，输出每对的相关性分数
        pairs = [[query, d] for d in docs]

        # 3. 调用模型的 `predict` 方法，得到每对的相关性分数（float 值，越高越相关）。
        scores = self._model.predict(pairs)

        # 4. 按分数从高到低对所有文档下标排序。
        order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)

        # 5. 截取前 top_k 个下标返回。
        return order[:top_k]
