"""OpenAlexSearchTool：OpenAlex API 搜索 + 可选开放获取 PDF 下载。

risk=medium（下载是写操作，保守过标——只读会话不触碰 vault）。SSRF 走 _http。

Task 3 增补（A1/A3/A4/A5/B2）：与 arxiv_search 同构——
- A1 年份用 publication_year filter 区间过滤（结构化参数，不拼进 search）
- A3 声明 wants_run_state，结果入 per-run 自动去重池
- A4 模块级 query 缓存（键前缀 "openalex"）
- A5 源熔断（源名 "openalex"）
- B2 max_results 钳制到 [3,50]；downloadable 由 best_oa_location 判定

2026-08-08 目录整理：execute() 骨架抽到 BaseSearchTool（search/_base.py，与
arxiv_search 共享模板方法）；本文件保留 OpenAlexClient 与源差异段。
"""
import urllib.parse

import httpx

from paperflow.core.security.network import validate_url_target
from paperflow.tools._http import _HttpClientMixin
from paperflow.tools.search._base import BaseSearchTool


class OpenAlexClient(_HttpClientMixin):
    def __init__(self, transport=None, ssrf_check=None):
        self.client = httpx.Client(transport=transport, timeout=30.0)
        self.ssrf_check = ssrf_check or validate_url_target

    def search(self, query: str, max_results: int = 5,
               year_from: int | None = None, year_to: int | None = None) -> list[dict]:
        params = {"search": query, "per-page": max_results}
        # A1：年份用 publication_year filter 区间过滤（半开区间，缺侧开放），
        # 不拼进自由文本 search——结构与关键词分离，避免 filter 词被当成检索词。
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
                # DOI 存进 dict：A3 去重键优先级最高（doi > arxiv_id > 规范化标题），
                # 跨源同论文靠它合并（openalex 常带 DOI，arxiv 预印本不带）。
                "doi": w.get("doi"),
                "venue": src.get("display_name"),          # A2：来源名（等级 lazy）
                "issn": (src.get("issn") or [None])[0],    # A2：ISSN 供精确查等级
                "venue_type": src.get("type"),             # journal / conference-proceedings
                "pdf_url": (w.get("best_oa_location") or {}).get("pdf_url"),
                # A2：是否可下载 = 有无 OA 位置（可下才提示下载，不可下静默跳过）
                "downloadable": bool((w.get("best_oa_location") or {}).get("pdf_url")),
                "arxiv_id": None,
            })
        return papers


class OpenAlexSearchTool(BaseSearchTool):
    name = "openalex_search"
    # IMPORTANT-3：description 与 execute 行为对齐——缺省不下载，传 download_to 才下载
    description = "搜索 OpenAlex 论文；可选下载开放获取 PDF（缺省不下载，传入 download_to 才下载）"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            # B2：schema 层给 minimum/maximum，让模型端 tool_use 就钳在合法区间
            "max_results": {"type": "integer", "default": 5,
                            "minimum": 3, "maximum": 50},
            "download_to": {"type": "string", "format": "path",
                            "description": "PDF 保存绝对路径（可选；缺省不下载，传入才下载）"},
            # A1：年份是结构化过滤参数，与自由文本 query 平级——不要拼进检索词
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
