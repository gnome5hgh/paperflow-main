# paperflow/rag/encoders/__init__.py
"""RAG 编码子包：稀疏 BM25 索引。稠密编码/精排协议统一收口 core/llm，本地模型实现已删除。"""
from paperflow.rag.encoders.bm25 import Bm25Index, tokenize

__all__ = ["Bm25Index", "tokenize"]
