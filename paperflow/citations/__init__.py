"""引用管理（溯源落地）：references.bib 读写 + 语料标题索引 + 编排。

懒加载单例 get_citation_manager()：工具/CLI 共享同一实例；重组件（corpus
索引、TitleExtractor）在 CitationManager 内部首次使用时才构造。
"""
from __future__ import annotations

import threading

from paperflow.citations.bib import BibEntry
from paperflow.citations.manager import CitationManager, ResolvedCitation, gen_key, entry_text

_singleton: CitationManager | None = None
_singleton_lock = threading.Lock()


def get_citation_manager(config=None) -> CitationManager:
    """懒加载单例（双重检查加锁）；config 首次传参后固定。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                if config is None:
                    from paperflow.config import PaperFlowConfig
                    config = PaperFlowConfig.from_env()
                _singleton = CitationManager(config)
    return _singleton


__all__ = ["CitationManager", "ResolvedCitation", "BibEntry", "gen_key", "entry_text",
           "get_citation_manager"]
