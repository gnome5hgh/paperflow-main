"""paperflow RAG 检索子系统：文档解析、向量/稀疏索引与混合检索。

对外统一导出解析器、服务门面与检索工具；编码器与向量存储等底层组件经由
各自子包访问。重依赖（chromadb、sentence-transformers）由 RAGService 内部
惰性加载，包导入本身不拉取，避免拖慢应用与测试启动。
"""
from paperflow.rag.parsers.chunker import AcademicChunker, Chunk
from paperflow.rag.parsers.grobid_client import GrobidClient, ParsedDoc, PyMuPDFParser
from paperflow.rag.services.rag_service import RAGService, get_rag_service
from paperflow.rag.services.indexer import RagIndexer
from paperflow.rag.services.retriever import Retriever, RagRetrieveTool

__all__ = ["AcademicChunker", "Chunk", "GrobidClient", "ParsedDoc", "PyMuPDFParser",
           "RAGService", "get_rag_service", "RagIndexer", "Retriever", "RagRetrieveTool"]
