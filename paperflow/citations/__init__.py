"""引用管理（溯源落地）：references.bib 读写 + 语料标题索引 + 编排。

分层：`constants/`（枚举）· `schemas/`（数据模型）· `storage/`（bib 文件读写
原语）· `parsers/`（pdf2bib 书目提取）· `services/`（编排、语料索引、key 生成规则）。
对外只经本门面进入——懒加载单例 get_citation_manager() 让工具/CLI 共享同一实例；
重组件（corpus 索引、书目提取器）在 CitationManager 内部首次使用时才构造。
"""
from __future__ import annotations

import threading

from paperflow.citations.schemas import BibEntry, ResolvedCitation
from paperflow.citations.services.keys import gen_key
from paperflow.citations.services.manager import CitationManager
from paperflow.citations.storage import entry_text

_singleton: CitationManager | None = None
_singleton_lock = threading.Lock()


def get_citation_manager(config=None) -> CitationManager:
    """懒加载单例（双重检查加锁）；config 首次传参后固定。

    Args:
        config: PaperFlowConfig | None，首次调用须传（后续传参被忽略）；None 时按环境加载配置

    Returns:
        进程内共享的 CitationManager 单例。
    """
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
