"""书目元数据提取子包：用 pdf2bib 从 PDF 取出作者 / 年份 / 期刊。"""
from .paper_meta_extract import BibMeta, PaperMetaExtractor

__all__ = ["BibMeta", "PaperMetaExtractor"]
