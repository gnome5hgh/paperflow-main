"""RAG 域的模型客户端：稠密编码与精排。

两者都是**云端的无状态 SDK 包装**（OpenAI 兼容端点），只服务 RAG——独立成一层是因为
它们既不是「编码器」（精排给的是分数不是向量），也不是有业务逻辑的服务（不持有状态、
不做重试决策之外的判断）。

协议（`Embedder` / `Reranker`）与实现同处一个文件：契约只有一个消费方（RAG 检索栈），
分开两处只会让「谁定义了契约」变得含糊。测试注入点是构造参数的 `transport`
（httpx 假传输），不触网。
"""
from paperflow.rag.models.embedder import Embedder, RagEmbedder
from paperflow.rag.models.reranker import RagReranker, Reranker

__all__ = ["Embedder", "RagEmbedder", "RagReranker", "Reranker"]
