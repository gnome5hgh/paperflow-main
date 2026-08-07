"""ArxivSearchTool：arXiv API 搜索 + 可选 PDF 下载。

risk=medium：下载 PDF 是写操作（tool.py 分类 medium = "下载 PDF"）；整工具标
medium 而非 low——否则会话降级到只读模式（max_risk=low）时下载仍可绕过写边界。
保守过标：搜索不下载也按 medium（该工具具备下载能力，只读会话本就不应触碰 vault）。
SSRF：每次出站抓取前 validate_url_target，重定向链走 _search_common 的 resolve_url_target。

Task 3 增补（A1/A3/A4/A5/B2）：
- A1 年份用 arXiv 原生 submittedDate 区间过滤（结构化参数，绝不拼进自由文本 query）
- A3 声明 wants_run_state，结果入 per-run 自动去重池（SearchRunState.add）
- A4 模块级 query 缓存：同 (源, query, 年份, max_results) 重复检索直接复用
- A5 源熔断：arXiv 连续失败 ≥2 次时 5 分钟内短路，提示改 openalex
- B2 max_results 钳制到 [3,50]（schema minimum/maximum + execute 兜底）
"""
import urllib.parse
import xml.etree.ElementTree as ET
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
# paperflow.tools.arxiv_search.get_rag_service 注入假服务。rag.service 不反向依赖 tools，无循环 import。
from paperflow.rag.service import get_rag_service
from paperflow.tools._search_common import _SearchClientMixin, _download_pdf

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class ArxivClient(_SearchClientMixin):
    def __init__(self, transport=None, ssrf_check=None):
        self.client = httpx.Client(transport=transport, timeout=30.0)
        self.ssrf_check = ssrf_check or validate_url_target

    def search(self, query: str, max_results: int = 5,
               year_from: int | None = None, year_to: int | None = None) -> list[dict]:
        # A1：年份用原生 submittedDate 区间过滤，绝不拼进自由文本 query——
        # 拼进 query 会被 arXiv 当关键词模糊匹配，且易被注入改写检索语义；
        # submittedDate 是 arXiv 官方字段，[lo TO hi] 是标准 Lucene 区间语法。
        # lo/hi 格式 YYYYMMDDHHMM：缺侧边界用全开（0000 年起 / 9999 年止）。
        if year_from or year_to:
            lo = f"{year_from}01010000" if year_from else "000001010000"
            hi = f"{year_to}12312359" if year_to else "999912312359"
            query = f'{query} AND submittedDate:[{lo} TO {hi}]'
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
                # year 供下游按年份过滤/排序；published 是完整 ISO 时间戳
                "year": int(published[:4]) if len(published) >= 4 else None,
                "abstract": " ".join((entry.find("atom:summary", _ATOM_NS).text or "").split()),
                "pdf_url": f"https://arxiv.org/pdf/{pid}",
                "venue": None, "issn": None,     # 预印本无 venue（A2 lazy 解析）
                "downloadable": True,            # arXiv 一律可下载（OA）
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
    # Task 3：声明后 _exec_tool 会按 trace_id 注入 _run_state（per-run 去重池）——
    # opt-in 注入，非声明工具零开销（不构造状态）。
    wants_run_state = True

    def __init__(self):
        super().__init__()
        self._client = None

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        return ArxivClient(transport=transport, ssrf_check=ssrf_check)

    def execute(self, query: str, max_results: int = 5, download_to: str | None = None,
                year_from: int | None = None, year_to: int | None = None,
                _run_state=None) -> ToolResult:
        # B2：execute 兜底钳制（模型可能绕过 schema 传 1 或 100）——越界数量会让
        # 下游 filter/review 超预算，clamp 到 [3,50] 保证结果规模可控。
        max_results = min(max(3, max_results), 50)
        # A4：缓存键 = (源, query, 年份区间, max_results)——不同源/参数视为不同检索。
        # 缓存在网络前：重复 query 直接复用结果，不重复打 API 也不重复入池。
        # 注意 ckey 用 clamp 后的 max_results，保证同一检索意图（如模型传 1 与 5）
        # 命中同一份缓存。
        ckey = ("arxiv", query, year_from, year_to, max_results)
        cached = query_cache_get(ckey)
        if cached is not None:
            return ToolResult(text=f"（缓存）该 query 已搜索过，结果同上；如需不同结果请调整检索词。\n{cached}")
        # A5：源熔断在缓存后、网络前——连续失败 ≥2 次时 5 分钟内不再打 arXiv API。
        # 工具不能自己换源，只能把"改用 openalex"的提示交给 LLM 决策。
        if breaker_is_open("arxiv"):
            return ToolResult(text="arXiv 连续失败已熔断，本次任务请改用 openalex_search")
        client = self._client or self._make_client()
        try:
            papers = client.search(query, max_results, year_from, year_to)
            breaker_register_success("arxiv")
        except SSRFError as e:
            # SSRF 违规：返回错误而非继续（LLM 可见，可自行调整目标）；
            # 不计入源熔断——是安全拦截，不是源故障。
            return ToolResult(text=f"SSRF blocked: {e}")
        except Exception as e:
            # httpx 超时/连接错误等真实源故障 → 计一次失败（A5）
            breaker_register_failure("arxiv")
            return ToolResult(text=f"Tool error: {e}")
        if _run_state is not None:
            # A3：结果入 per-run 自动去重池（SearchRunState.add 内部按
            # DOI→arXiv ID→规范化标题 四级键去重合并）。_run_state 是注入的保留
            # kwarg，绝不存进任何会被序列化的字段（审计 ctx.args 会带它，若审计
            # 报错是 after 钩子的优雅降级路径，不是本工具缺陷）。
            _run_state.add(papers)
        lines = [f"- [{p['title']}] ({p['year']}) venue={p['venue']} issn={p['issn']} pdf={p['pdf_url']} 来源=arxiv" for p in papers]
        query_cache_put(ckey, "\n".join(lines))      # A4：缓存本次结果供重复 query 复用
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
