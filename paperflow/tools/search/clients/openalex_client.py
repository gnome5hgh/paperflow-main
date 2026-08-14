"""OpenAlexClient：OpenAlex API 纯搜索客户端（工具本体在 web_search.py）。

SSRF 校验走共享的 paperflow/tools/common/_http.py。只读操作,写盘/下载由
FetchPdfTool 承担。本文件只保留客户端(含年份区间过滤与开放获取判定);
WebSearchTool 经 _SOURCE_REGISTRY 按 source 分发到本客户端。
"""
import urllib.parse

import httpx

from paperflow.core.security.network import validate_url_target
from paperflow.tools.common._http import _HttpClientMixin


class OpenAlexClient(_HttpClientMixin):
    def __init__(self, transport=None, ssrf_check=None):
        """建 httpx 同步客户端;transport/ssrf_check 供测试注入 MockTransport 与 SSRF 桩。"""
        self.client = httpx.Client(transport=transport, timeout=30.0)
        self.ssrf_check = ssrf_check or validate_url_target

    def search(self, query: str, max_results: int = 5,
               year_from: int | None = None, year_to: int | None = None) -> list[dict]:
        """调 OpenAlex API 搜索论文,返回论文 dict 列表。

        年份用 publication_year filter 区间过滤(半开区间,缺侧开放),与检索词分离;
        DOI 是跨源去重最高优先级键(openalex 常带而 arxiv 不带)。可下载 = 有开放获取
        位置,否则 downloadable=False(下载门禁据此静默跳过)。
        """
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
