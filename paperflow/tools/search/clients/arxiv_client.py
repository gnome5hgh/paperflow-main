"""ArxivClient：arXiv API 纯搜索客户端（工具本体在 web_search.py）。

出站抓取前做 SSRF 校验,重定向链逐跳校验(见 paperflow/tools/common/_http.py)。
只读操作,写盘/下载由 FetchPdfTool 承担。本文件只保留客户端与年份区间过滤;
WebSearchTool 经 _SOURCE_REGISTRY 按 source 分发到本客户端。
"""
import urllib.parse
import xml.etree.ElementTree as ET

import httpx

from paperflow.core.security.network import validate_url_target
from paperflow.tools.common._http import _HttpClientMixin

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _normalize_arxiv_query(query: str) -> str:
    """裸关键词转 arXiv 字段前缀形式:'graph algorithm' → 'all:graph AND all:algorithm'。

    只有带字段前缀才能与 submittedDate 组合——arXiv 已知缺陷:裸多词 AND submittedDate
    会静默丢弃日期过滤(返回旧论文),前缀 + 显式 AND 才生效。仅在需要日期过滤时调用;
    无日期过滤的纯搜索保持原样(不带前缀也正确)。
    """
    terms = [t for t in query.split() if t]
    return " AND ".join(f"all:{t}" for t in terms) if terms else query


class ArxivClient(_HttpClientMixin):
    def __init__(self, transport=None, ssrf_check=None):
        self.client = httpx.Client(transport=transport, timeout=30.0)
        self.ssrf_check = ssrf_check or validate_url_target

    def search(self, query: str, max_results: int = 5,
               year_from: int | None = None, year_to: int | None = None) -> list[dict]:
        # 年份用 arXiv 原生 submittedDate 区间过滤,绝不拼进自由文本 query——拼进去会被
        # arXiv 当关键词模糊匹配,且易被注入改写检索语义。submittedDate 是官方字段,
        # [lo TO hi] 是标准 Lucene 区间语法;缺侧边界用全开(0000 年起 / 9999 年止)。
        # 注意裸多词 + submittedDate 会被 arXiv 静默丢弃日期过滤,必须先转 all: 前缀。
        if year_from or year_to:
            lo = f"{year_from}01010000" if year_from else "000001010000"
            hi = f"{year_to}12312359" if year_to else "999912312359"
            query = f"{_normalize_arxiv_query(query)} AND submittedDate:[{lo} TO {hi}]"
        url = ("http://export.arxiv.org/api/query?" +
               urllib.parse.urlencode({"search_query": query,
                                       "max_results": max_results}))
        r = self._get(url)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        papers = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            title = entry.find("atom:title", _ATOM_NS).text or ""
            pid = (entry.find("atom:id", _ATOM_NS).text or "").split("/abs/")[-1]
            published = entry.find("atom:published", _ATOM_NS).text or ""
            papers.append({
                "title": " ".join(title.split()),
                "arxiv_id": pid,
                "published": published,
                # year 供下游按年份过滤/排序;published 是完整 ISO 时间戳
                "year": int(published[:4]) if len(published) >= 4 else None,
                "abstract": " ".join((entry.find("atom:summary", _ATOM_NS).text or "").split()),
                "pdf_url": f"https://arxiv.org/pdf/{pid}",
                "venue": None, "issn": None,     # 预印本无 venue(等级由 reviewer 后查)
                "downloadable": True,            # arXiv 一律可下载(开放获取)
            })
        return papers
