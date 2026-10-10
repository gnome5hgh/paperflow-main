"""RAG 领域实体与值对象。

「一个东西」的模型：检索块（Chunk）与章节（Section）。与 `dto/` 的分界是
「是不是 RAG 业务里的东西本身」——实体回答「系统里有什么」，dto 只负责把数据
从一层搬到另一层。
"""
from paperflow.rag.domain.entity.chunk import (
    Chunk,
    context_prefix,
    indexed_text,
    section_label,
)
from paperflow.rag.domain.entity.section import Section

__all__ = ["Chunk", "Section", "context_prefix", "indexed_text", "section_label"]
