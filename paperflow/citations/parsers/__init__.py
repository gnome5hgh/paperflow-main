"""首页书目元数据提取子包：把 PDF 首页文本交给模型，取作者 / 年份 / 期刊。"""
from .paper_meta_extract import BibMeta, PaperMetaExtractor

__all__ = ["BibMeta", "PaperMetaExtractor"]
