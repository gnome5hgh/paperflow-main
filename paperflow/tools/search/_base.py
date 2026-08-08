# paperflow/tools/search/_base.py
"""BaseSearchTool：arxiv/openalex 双源搜索的共享骨架（A1/A3/A4/A5/B2）。

两源 execute() 骨架逐段相同：B2 clamp → A4 缓存 → A5 熔断 → SSRF 抓取 → A3 去重
入池 → 缓存写 → 下载 → 热更新索引。2026-08-08 目录整理收敛为模板方法；子类只实现
差异段：
- ``_source`` / ``_breaker_name`` / ``_alternate``：熔断 key 与缓存键的源标识
- ``_make_client``：各自 client（测试注入 MockTransport + ssrf 桩）
- ``_render_lines``：结果行格式化（来源标记差异）
- ``_download_url``：下载目标（默认取首篇 pdf_url；openalex 无 OA 地址时返回
  None → 跳过下载，即旧代码的 ``papers[0].get("pdf_url")`` 守卫）

SSRF 安全抓取与 PDF 下载助手在 paperflow/tools/_http.py（search/ 与 rank/ 共用）。
get_rag_service 在本模块绑定（下载后 index_document 热更新）——测试 monkeypatch
目标为 ``paperflow.tools.search._base.get_rag_service``。
"""
from pathlib import Path

from paperflow.core.search_state import (
    query_cache_get, query_cache_put,
    breaker_is_open, breaker_register_failure, breaker_register_success,
)
from paperflow.core.security.network import SSRFError
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service
from paperflow.tools._http import _download_pdf


class BaseSearchTool(Tool):
    """双源搜索共享骨架（模板方法）。子类覆写 _source/_make_client/_render_lines。"""

    #: 搜索源名：熔断 key + 缓存键前缀（子类覆写为 "arxiv"/"openalex"）
    _source: str = ""
    #: 熔断提示显示名（子类覆写，如 "arXiv"）
    _breaker_name: str = ""
    #: 熔断时建议的备选工具名（子类覆写，如 "openalex_search"）
    _alternate: str = ""

    #: 下载是写操作，保守过标——只读会话（max_risk=low）不触碰 vault
    risk_level = "medium"
    allowed_roots = ["pdf"]
    #: 返回外部内容（标题/摘要/URL）→ 未校验横幅
    output_scan = "mark"
    side_effects = ["network", "write_file"]
    #: A3：声明后 _exec_tool 按 trace_id 注入 _run_state（per-run 去重池）——
    #: opt-in 注入，非声明工具零开销（不构造状态）。
    wants_run_state = True

    def __init__(self):
        super().__init__()
        # 懒建客户端：缓存命中/熔断短路不走网络（测试可 _make_client 注入 MockTransport）
        self._client = None

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        """构造源 client；测试经 transport/ssrf_check 注入 MockTransport + SSRF 桩。"""
        raise NotImplementedError

    def _render_lines(self, papers: list[dict]) -> list[str]:
        """把搜索结果渲染成 LLM 可见行（含来源标记；空结果返回空列表）。"""
        raise NotImplementedError

    def _download_url(self, papers: list[dict]) -> str | None:
        """返回要下载的 PDF URL；None = 跳过下载。

        默认取首篇 pdf_url——arxiv 恒有（预印本 OA），openalex 无 OA 地址时
        pdf_url 为 None → 跳过（旧代码的 ``papers[0].get("pdf_url")`` 守卫）。
        """
        return papers[0].get("pdf_url") if papers else None

    def execute(self, query: str, max_results: int = 5, download_to: str | None = None,
                year_from: int | None = None, year_to: int | None = None,
                _run_state=None) -> ToolResult:
        # B2：execute 兜底钳制（模型可能绕过 schema 传 1 或 100）——越界数量会让
        # 下游 reviewer 门禁超预算，clamp 到 [3,50] 保证结果规模可控。
        max_results = min(max(3, max_results), 50)
        # A4：缓存键 = (源, query, 年份区间, max_results)——不同源/参数视为不同检索。
        # 缓存在网络前：重复 query 直接复用结果，不重复打 API 也不重复入池。
        # ckey 用 clamp 后的 max_results，保证同一检索意图（如模型传 1 与 5）命中同份缓存。
        ckey = (self._source, query, year_from, year_to, max_results)
        cached = query_cache_get(ckey)
        # 缓存命中短路只对纯搜索生效：带 download_to 的调用即使缓存命中也要走网络+下载
        # ——下游 C1 下载流程是「先搜索→门禁→同 query 复调 download_to」，第二次调用若
        # 被缓存短路，PDF 永远不会落盘。入池路径照旧只在真实网络调用后 add。
        if cached is not None and download_to is None:
            return ToolResult(text=f"（缓存）该 query 已搜索过，结果同上；如需不同结果请调整检索词。\n{cached}")
        # A5：源熔断在缓存后、网络前——连续失败 ≥2 次时 5 分钟内不再打本源 API。
        # 工具不能自己换源，只能把"改用备选源"的提示交给 LLM 决策。
        if breaker_is_open(self._source):
            return ToolResult(text=f"{self._breaker_name} 连续失败已熔断，本次任务请改用 {self._alternate}")
        client = self._client or self._make_client()
        try:
            papers = client.search(query, max_results, year_from, year_to)
            breaker_register_success(self._source)
        except SSRFError as e:
            # SSRF 违规：返回错误而非继续（LLM 可见，可自行调整目标）；
            # 不计入源熔断——是安全拦截，不是源故障。
            return ToolResult(text=f"SSRF blocked: {e}")
        except Exception as e:
            # httpx 超时/连接错误等真实源故障 → 计一次失败（A5）
            breaker_register_failure(self._source)
            return ToolResult(text=f"Tool error: {e}")
        if _run_state is not None:
            # A3：结果入 per-run 自动去重池（SearchRunState.add 内部按
            # DOI→arXiv ID→规范化标题 四级键去重合并）。_run_state 由 _exec_tool
            # 作为独立 kwarg 直传 execute（不写进 ctx.args，避免审计序列化污染）。
            _run_state.add(papers)
        lines = self._render_lines(papers)
        # 仅非空结果入缓存——空结果若缓存，重复 query 会命中返回「（缓存）…」而非
        # 「无搜索结果」，语义不一致。
        if papers:
            query_cache_put(ckey, "\n".join(lines))
        pdf_url = self._download_url(papers)
        if download_to and pdf_url:
            dest = Path(download_to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                _download_pdf(client, pdf_url, dest)
            except Exception as e:
                # 含 SSRFError/RuntimeError/ValueError（非 PDF 响应/重定向未解析完整）
                return ToolResult(text=f"下载失败: {e}")
            get_rag_service().index_document(str(dest))   # 热更新钩子
            lines.append(f"已下载 PDF: {dest}")
        return ToolResult(text="\n".join(lines) if lines else "无搜索结果")
