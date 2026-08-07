"""OpenAlexSearchTool：OpenAlex API 搜索 + 可选开放获取 PDF 下载。

risk=medium（下载是写操作，保守过标——只读会话不触碰 vault）。SSRF 走 _search_common。

Task 3 增补（A1/A3/A4/A5/B2）：与 arxiv_search 同构——
- A1 年份用 publication_year filter 区间过滤（结构化参数，不拼进 search）
- A3 声明 wants_run_state，结果入 per-run 自动去重池
- A4 模块级 query 缓存（键前缀 "openalex"）
- A5 源熔断（源名 "openalex"）
- B2 max_results 钳制到 [3,50]；downloadable 由 best_oa_location 判定
"""
import urllib.parse
from pathlib import Path

import httpx

from paperflow.core.security.network import (
    validate_url_target, SSRFError,
)
from paperflow.core.search_state import (
    query_cache_get, query_cache_put,
    breaker_is_open, breaker_register_failure, breaker_register_success,
)
from paperflow.core.tool import Tool, ToolResult
# 模块级绑定 get_rag_service：execute 内直接引用模块全局，测试可 monkeypatch
# paperflow.tools.openalex_search.get_rag_service 注入假服务。
from paperflow.rag.service import get_rag_service
from paperflow.tools._search_common import _SearchClientMixin, _download_pdf


class OpenAlexClient(_SearchClientMixin):
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


class OpenAlexSearchTool(Tool):
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
    risk_level = "medium"
    allowed_roots = ["pdf"]
    output_scan = "mark"                       # MINOR-7：返回外部内容（标题/摘要/URL）→ 未校验横幅
    side_effects = ["network", "write_file"]
    # Task 3：声明后 _exec_tool 会按 trace_id 注入 _run_state（per-run 去重池）。
    wants_run_state = True

    def __init__(self):
        super().__init__()
        self._client = None

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        return OpenAlexClient(transport=transport, ssrf_check=ssrf_check)

    def execute(self, query: str, max_results: int = 5, download_to: str | None = None,
                year_from: int | None = None, year_to: int | None = None,
                _run_state=None) -> ToolResult:
        # B2：execute 兜底钳制到 [3,50]（同 arxiv_search，理由一致）
        max_results = min(max(3, max_results), 50)
        # A4：缓存键 = (源, query, 年份区间, max_results)——openalex 与 arxiv 是
        # 不同源，键前缀分开，避免同 query 跨源误命中。
        ckey = ("openalex", query, year_from, year_to, max_results)
        cached = query_cache_get(ckey)
        # review finding 1：缓存命中短路只对纯搜索生效（理由同 arxiv_search——
        # 带 download_to 的 C1 下载流程绝不能被缓存短路）。
        if cached is not None and download_to is None:
            return ToolResult(text=f"（缓存）该 query 已搜索过，结果同上；如需不同结果请调整检索词。\n{cached}")
        # A5：源熔断在缓存后、网络前——openalex 连续失败时短路，提示改 arxiv。
        if breaker_is_open("openalex"):
            return ToolResult(text="OpenAlex 连续失败已熔断，本次任务请改用 arxiv_search")
        client = self._client or self._make_client()
        try:
            papers = client.search(query, max_results, year_from, year_to)
            breaker_register_success("openalex")
        except SSRFError as e:
            # SSRF 违规不计入源熔断（安全拦截非源故障）
            return ToolResult(text=f"SSRF blocked: {e}")
        except Exception as e:
            breaker_register_failure("openalex")
            return ToolResult(text=f"Tool error: {e}")
        if _run_state is not None:
            _run_state.add(papers)                   # A3：自动去重入池（同 arxiv 注释）
        lines = [f"- [{p['title']}] ({p['year']}) venue={p['venue']} issn={p['issn']} pdf={p['pdf_url'] or 'no OA'} 来源=openalex" for p in papers]
        # review finding 4：仅非空结果入缓存（理由同 arxiv_search）
        if papers:
            query_cache_put(ckey, "\n".join(lines))      # A4：缓存本次结果
        if download_to and papers and papers[0].get("pdf_url"):
            # 只有有 OA 地址才尝试下载（downloadable 判定）；其余静默跳过
            dest = Path(download_to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                _download_pdf(client, papers[0]["pdf_url"], dest)
            except Exception as e:
                return ToolResult(text=f"下载失败: {e}")   # 含 SSRFError/RuntimeError/ValueError
            get_rag_service().index_document(str(dest))   # 热更新钩子
            lines.append(f"已下载 PDF: {dest}")
        return ToolResult(text="\n".join(lines) if lines else "无搜索结果")
