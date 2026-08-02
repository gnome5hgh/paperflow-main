# paperflow/tools/search.py
"""搜索类原子 Tool：arXiv/OpenAlex 检索 + PDF 下载 + 去重/筛选。

risk=medium：下载 PDF 是写操作（tool.py 分类 medium = "下载 PDF"）；整工具标
medium 而非 low——否则会话降级到只读模式（max_risk=low）时下载仍可绕过写边界。
保守过标：搜索不下载也按 medium（该工具具备下载能力，只读会话本就不应触碰 vault）。
SSRF：每次出站抓取前 validate_url_target，重定向链走 resolve_url_target。
"""
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from paperflow.core.security.network import (
    validate_url_target, resolve_url_target, SSRFError,
)
from paperflow.core.tool import Tool, ToolResult
# 模块级绑定 get_rag_service（与 tools/file.py 一致）：execute 内直接引用模块
# 全局，测试可 monkeypatch paperflow.tools.search.get_rag_service 注入假服务。
# rag.service 不反向依赖 tools，无循环 import 风险。
from paperflow.rag.service import get_rag_service

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class _SearchClientMixin:
    """共享：SSRF 校验 + httpx 同步客户端（工具已在线程池跑）。"""

    def _get(self, url: str):
        self.ssrf_check(url)
        url = resolve_url_target(url)          # 重定向逐跳校验（httpx 已是硬依赖）
        self.ssrf_check(url)
        return self.client.get(url)


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


def _download_pdf(client, url: str, dest: Path) -> None:
    """共享下载助手：逐跳 SSRF 校验重定向链，绝不把 3xx 或非 PDF 响应体写盘。

    spec §13：重定向链走 resolve_url_target。两个搜索分支共用此路径，
    避免各自写下载逻辑再引入同类缺陷。
    """
    resolved = resolve_url_target(url)      # HEAD 逐跳 SSRF 校验，返回最终 URL
    client.ssrf_check(resolved)             # 最终 URL 也校验（validate_url_target 要求公网 IP，私网全拒）
    r = client.client.get(resolved, follow_redirects=False)
    if r.is_redirect:
        # HEAD 与 GET 的重定向路径可能分叉（签名/方法相关）；残余 3xx 绝不写盘
        raise RuntimeError(f"重定向未解析完整: {url} -> {resolved}")
    r.raise_for_status()                    # 4xx/5xx
    if not r.content.startswith(b"%PDF"):
        # 服务器 200 但返回 HTML（错误页/登录墙）——magic bytes 比 content-type 可靠
        raise ValueError(f"响应不是 PDF（缺 %PDF magic bytes）: {url}")
    dest.write_bytes(r.content)


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


class DedupPapersTool(Tool):
    """多源去重：DOI → arXiv ID → 规范化标题 四级匹配。纯逻辑、无副作用。"""

    name = "dedup_papers"
    description = "对多来源论文列表去重（DOI → arXiv ID → 标题四级匹配）"
    parameters = {
        "type": "object",
        "properties": {
            "papers": {"type": "array", "items": {"type": "object"},
                       "description": "论文元数据列表"},
        },
        "required": ["papers"],
    }
    risk_level = "low"

    @staticmethod
    def _norm_title(title: str) -> str:
        import re
        return re.sub(r"[^\w]", "", title).lower()

    def execute(self, papers: list[dict]) -> ToolResult:
        # 四级匹配：DOI 最可靠，其次 arXiv ID，最后规范化标题（退化键）。
        # 多源搜索可能返回同一论文的不同版本（标题带版本号/期刊名后缀），
        # 先按强键合并，避免后续 Filter 阶段重复计算。
        seen_doi, seen_arxiv, seen_title = set(), set(), set()
        merged = []
        for p in papers:
            key = None
            if p.get("doi"):
                key = ("doi", p["doi"])
            elif p.get("arxiv_id"):
                key = ("arxiv", p["arxiv_id"])
            else:
                key = ("title", self._norm_title(p.get("title", "")))
            sig, val = key
            bucket = seen_doi if sig == "doi" else (seen_arxiv if sig == "arxiv" else seen_title)
            if val in bucket:
                continue
            bucket.add(val)
            merged.append(p)
        # 输出带上 arXiv ID（若有）：让 LLM 可观测"同一 arXiv ID 只留一条"的去重结果
        # （brief 测试断言 arxiv_id 在文本中出现一次）。缺 arXiv ID 的条目只打标题，
        # 避免输出 "None" 噪音。
        lines = [f"- {p.get('title')} ({p['arxiv_id']})" if p.get("arxiv_id")
                 else f"- {p.get('title')}" for p in merged]
        return ToolResult(text="\n".join(lines) if lines else "（空列表）")


class FilterPapersTool(Tool):
    """综合筛选：年份/引用数/期刊白名单。阈值由 LLM 传参；期刊白名单可省略。"""

    name = "filter_papers"
    description = "按年份/引用数/期刊筛选论文列表"
    parameters = {
        "type": "object",
        "properties": {
            "papers": {"type": "array", "items": {"type": "object"}},
            "year_min": {"type": "integer", "description": "最小年份（可选）"},
            "min_citations": {"type": "integer", "description": "最小引用数（可选）"},
            "journals": {"type": "array", "items": {"type": "string"},
                         "description": "期刊白名单（可选）"},
        },
        "required": ["papers"],
    }
    risk_level = "low"

    def execute(self, papers: list[dict], year_min: int | None = None,
                min_citations: int | None = None,
                journals: list[str] | None = None) -> ToolResult:
        out = []
        for p in papers:
            if year_min is not None and (p.get("year") or 0) < year_min:
                continue
            if min_citations is not None and (p.get("cited_by_count") or 0) < min_citations:
                continue
            # journals 白名单可省略：p 缺 journal 字段时 (p.get("journal") is None)
            # 不 ∈ journals → 会被筛掉。但 OpenAlex 条目无 journal 信息是常态，
            # 若 LLM 未传白名单就不应自动淘汰，故用 `journals and ...` 短路。
            if journals and p.get("journal") not in journals:
                continue
            out.append(p)
        lines = [f"- {p.get('title')}" for p in out]
        return ToolResult(text="\n".join(lines) if lines else "（无满足条件的论文）")
