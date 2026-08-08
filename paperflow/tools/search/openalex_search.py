"""OpenAlexSearchTool：OpenAlex API 纯搜索工具（下载已拆至 fetch_pdf）。

SSRF 校验走共享的 paperflow/tools/_http.py。只读操作,写盘/下载由
FetchPdfTool 承担。

execute() 骨架在 BaseSearchTool(search/_common.py)中与 arxiv 共享;本文件保留
OpenAlexClient(含年份区间过滤与开放获取判定)与来源差异段。
"""
import urllib.parse

import httpx

from paperflow.core.security.network import validate_url_target
from paperflow.tools._http import _HttpClientMixin
from paperflow.tools.search._common import BaseSearchTool


class OpenAlexClient(_HttpClientMixin):
    def __init__(self, transport=None, ssrf_check=None):
        self.client = httpx.Client(transport=transport, timeout=30.0)
        self.ssrf_check = ssrf_check or validate_url_target

    def search(self, query: str, max_results: int = 5,
               year_from: int | None = None, year_to: int | None = None) -> list[dict]:
        params = {"search": query, "per-page": max_results}
        # 年份用 publication_year filter 区间过滤(半开区间,缺侧开放),不拼进自由文本
        # search——结构与关键词分离,避免过滤词被当成检索词。
        if year_from or year_to:
            params["filter"] = f"publication_year:{year_from or ''}-{year_to or ''}"
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        r = self._get(url)
        r.raise_for_status()
        papers = []
        for w in r.json().get("results", []):
            src = (w.get("primary_location") or {}).get("source") or {}
            papers.append({
                "title": w.get("display_name", ""),
                "year": w.get("publication_year"),
                "cited_by_count": w.get("cited_by_count", 0),
                "openalex_id": w.get("id", ""),
                # DOI 是跨源去重优先级最高的键(doi > arxiv_id > 规范化标题):openalex
                # 常带 DOI,arxiv 预印本不带,同论文跨源靠它合并
                "doi": w.get("doi"),
                "venue": src.get("display_name"),          # 来源名(等级由 reviewer 后查)
                "issn": (src.get("issn") or [None])[0],    # ISSN 供精确查等级,避开同名歧义
                "venue_type": src.get("type"),             # journal / conference-proceedings
                "pdf_url": (w.get("best_oa_location") or {}).get("pdf_url"),
                # 是否可下载 = 有无开放获取位置(可下才提示下载,不可下静默跳过)
                "downloadable": bool((w.get("best_oa_location") or {}).get("pdf_url")),
                "arxiv_id": None,
            })
        return papers


class OpenAlexSearchTool(BaseSearchTool):
    name = "openalex_search"
    description = "搜索 OpenAlex 论文"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            # schema 层给 max_results 上下限,让模型端发起调用时就钳在合法区间
            "max_results": {"type": "integer", "default": 5,
                            "minimum": 3, "maximum": 50},
            # 年份是结构化过滤参数,与自由文本 query 平级——不要拼进检索词
            "year_from": {"type": "integer", "description": "起始年份（含），用此参数而非拼进 query"},
            "year_to": {"type": "integer", "description": "结束年份（含），用此参数而非拼进 query"},
        },
        "required": ["query"],
    }
    _source = "openalex"
    _breaker_name = "OpenAlex"
    _alternate = "arxiv_search"

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        return OpenAlexClient(transport=transport, ssrf_check=ssrf_check)

    def _render_lines(self, papers: list[dict]) -> list[str]:
        return [f"- [{p['title']}] ({p['year']}) venue={p['venue']} issn={p['issn']} pdf={p['pdf_url'] or 'no OA'} 来源=openalex" for p in papers]
