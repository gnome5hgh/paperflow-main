"""OpenAlexSearchTool：OpenAlex API 搜索 + 可选开放获取 PDF 下载。

risk=medium（下载是写操作，保守过标——只读会话不触碰 vault）。SSRF 走 _search_common。
"""
import urllib.parse
from pathlib import Path

import httpx

from paperflow.core.security.network import (
    validate_url_target, SSRFError,
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

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        url = ("https://api.openalex.org/works?" +
               urllib.parse.urlencode({"search": query, "per-page": max_results}))
        r = self._get(url)
        r.raise_for_status()
        papers = []
        for w in r.json().get("results", []):
            papers.append({
                "title": w.get("display_name", ""),
                "year": w.get("publication_year"),
                "cited_by_count": w.get("cited_by_count", 0),
                "openalex_id": w.get("id", ""),
                "pdf_url": (w.get("best_oa_location") or {}).get("pdf_url"),
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
            "max_results": {"type": "integer", "default": 5},
            "download_to": {"type": "string", "format": "path",
                            "description": "PDF 保存绝对路径（可选；缺省不下载，传入才下载）"},
        },
        "required": ["query"],
    }
    risk_level = "medium"
    allowed_roots = ["pdf"]
    output_scan = "mark"                       # MINOR-7：返回外部内容（标题/摘要/URL）→ 未校验横幅
    side_effects = ["network", "write_file"]

    def __init__(self):
        super().__init__()
        self._client = None

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        return OpenAlexClient(transport=transport, ssrf_check=ssrf_check)

    def execute(self, query: str, max_results: int = 5, download_to: str | None = None) -> ToolResult:
        client = self._client or self._make_client()
        try:
            papers = client.search(query, max_results)
        except SSRFError as e:
            return ToolResult(text=f"SSRF blocked: {e}")
        lines = [f"- [{p['title']}] ({p.get('pdf_url') or 'no OA'})" for p in papers]
        if download_to and papers and papers[0].get("pdf_url"):
            dest = Path(download_to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                _download_pdf(client, papers[0]["pdf_url"], dest)
            except Exception as e:
                return ToolResult(text=f"下载失败: {e}")   # 含 SSRFError/RuntimeError/ValueError
            get_rag_service().index_document(str(dest))   # 热更新钩子
            lines.append(f"已下载 PDF: {dest}")
        return ToolResult(text="\n".join(lines) if lines else "无搜索结果")
