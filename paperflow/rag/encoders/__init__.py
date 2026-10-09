# paperflow/rag/encoders/__init__.py
"""RAG 编码子包：把文本变成检索信号的三种编码器。

- 稀疏（`bm25.py`）：本地 jieba + rank_bm25，**有状态**——向量库全部文本的内存投影，
  重启后为空、需从库重建
- 稠密（`embedder.py`）：云端 bi-encoder（OpenAI 兼容 `/v1/embeddings`），文本 → L2 归一化向量
- 交叉精排（`reranker.py`）：云端 cross-encoder（`/v1/rerank`），query + 候选 → 按相关度降序的下标

三者产出形态不同（稀疏权重 / 向量 / 分数），但同属编码器家族：精排给的是分数不是向量，
cross-encoder 仍是标准叫法，故与另两件并列而非另立一层。后两者是无状态 SDK 包装，差别只在
跟模型说话的方式；协议（`Embedder` / `Reranker`）与实现同处一个文件，因为契约只有 RAG 检索栈
一个消费方，分开放两处只会让「谁定义契约」变含糊。测试注入点是构造参数的 `transport`
（httpx 假传输），不触网。

三件都在此再导出，导入任一件会一并拉起 jieba/rank_bm25 与 httpx；RAGService 三件都要用，
不为此拆开。
"""
from paperflow.rag.encoders.bm25 import Bm25Index, tokenize
from paperflow.rag.encoders.embedder import Embedder, RagEmbedder
from paperflow.rag.encoders.reranker import RagReranker, Reranker

__all__ = ["Bm25Index", "tokenize", "Embedder", "RagEmbedder", "RagReranker", "Reranker"]
