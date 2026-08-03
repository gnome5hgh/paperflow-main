"""ArxivSearchTool：arXiv API 搜索 + 可选 PDF 下载。

risk=medium：下载 PDF 是写操作（tool.py 分类 medium = "下载 PDF"）；整工具标
medium 而非 low——否则会话降级到只读模式（max_risk=low）时下载仍可绕过写边界。
保守过标：搜索不下载也按 medium（该工具具备下载能力，只读会话本就不应触碰 vault）。
SSRF：每次出站抓取前 validate_url_target，重定向链走 _search_common 的 resolve_url_target。
"""
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from paperflow.core.security.network import (
    validate_url_target, SSRFError,
)
from paperflow.core.tool import Tool, ToolResult
# 模块级绑定 get_rag_service：execute 内直接引用模块全局，测试可 monkeypatch
# paperflow.tools.arxiv_search.get_rag_service 注入假服务。rag.service 不反向依赖 tools，无循环 import。
from paperflow.rag.service import get_rag_service
from paperflow.tools._search_common import _SearchClientMixin, _download_pdf

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class ArxivClient(_SearchClientMixin):
    def __init__(self, transport=None, ssrf_check=None):
        self.client = httpx.Client(transport=transport, timeout=30.0)
        self.ssrf_check = ssrf_check or validate_url_target

    def search(self, query: str, max_results: int = 5) -> list[dict]:
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
            papers.append({
                "title": " ".join(title.split()),
                "arxiv_id": pid,
                "published": (entry.find("atom:published", _ATOM_NS).text or ""),
                "abstract": " ".join((entry.find("atom:summary", _ATOM_NS).text or "").split()),
                "pdf_url": f"https://arxiv.org/pdf/{pid}",
            })
        return papers


class ArxivSearchTool(Tool):
    name = "arxiv_search"
    # IMPORTANT-3：description 与 execute 行为对齐——缺省不下载，传 download_to 才下载
    description = "搜索 arXiv 论文；可选下载 PDF（缺省不下载，传入 download_to 才下载）"
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
        return ArxivClient(transport=transport, ssrf_check=ssrf_check)

    def execute(self, query: str, max_results: int = 5, download_to: str | None = None) -> ToolResult:
        client = self._client or self._make_client()
        try:
            papers = client.search(query, max_results)
        except SSRFError as e:
            # SSRF 违规：返回错误而非继续（LLM 可见，可自行调整目标）
            return ToolResult(text=f"SSRF blocked: {e}")
        lines = [f"- [{p['title']}] ({p['pdf_url']})" for p in papers]
        if download_to and papers:
            dest = Path(download_to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                _download_pdf(client, papers[0]["pdf_url"], dest)
            except Exception as e:
                return ToolResult(text=f"下载失败: {e}")   # 含 SSRFError/RuntimeError/ValueError
            get_rag_service().index_document(str(dest))   # 热更新钩子
            lines.append(f"已下载 PDF: {dest}")
        return ToolResult(text="\n".join(lines) if lines else "无搜索结果")
