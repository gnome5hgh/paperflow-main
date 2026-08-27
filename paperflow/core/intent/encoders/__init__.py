"""意图编码子包：稀疏编码器与混合索引（稠密编码复用 RAG 栈的 Embedder/BgeEmbedder）。"""
from paperflow.core.intent.encoders.bm25 import BM25Encoder, JiebaTokenizer
from paperflow.core.intent.encoders.index import HybridLocalIndex

__all__ = ["BM25Encoder", "JiebaTokenizer", "HybridLocalIndex"]
