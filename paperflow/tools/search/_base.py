# paperflow/tools/search/_base.py
"""BaseSearchTool：arxiv/openalex 双源搜索的共享骨架。

两个搜索源的 execute() 流程骨架完全相同:数量钳制 → 查询缓存 → 源熔断 → SSRF
安全抓取 → 结果去重入池 → 写缓存 → 下载 → 热更新索引。收敛为模板方法,子类只
实现差异段:
- ``_source`` / ``_breaker_name`` / ``_alternate``:熔断与缓存用的源标识
- ``_make_client``:各自的搜索客户端(测试可注入 MockTransport 与 SSRF 桩)
- ``_render_lines``:把结果渲染成 LLM 可见行(来源标记差异)
- ``_download_url``:返回要下载的 PDF 地址;openalex 无开放获取地址时为 None → 跳过下载

SSRF 安全抓取与 PDF 下载助手在 paperflow/tools/_http.py(search/ 与 rank/ 共用)。
get_rag_service 在本模块绑定(下载后做索引热更新)——测试以
``paperflow.tools.search._base.get_rag_service`` 为 monkeypatch 目标。
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
    """双源搜索共享骨架(模板方法)。子类覆写 _source/_make_client/_render_lines。"""

    #: 搜索源名:熔断键与缓存键的前缀(子类覆写为 "arxiv"/"openalex")
    _source: str = ""
    #: 熔断提示里的显示名(子类覆写,如 "arXiv")
    _breaker_name: str = ""
    #: 熔断时建议换用的备选工具名(子类覆写,如 "openalex_search")
    _alternate: str = ""

    #: 下载是写操作,保守过标——只读会话(风险上限为 low)也不该触碰本地资料库
    risk_level = "medium"
    allowed_roots = ["pdf"]
    #: 返回外部内容(标题/摘要/URL),需打"未经安全校验"横幅
    output_scan = "mark"
    side_effects = ["network", "write_file"]
    #: 声明后执行器会按追踪 ID 注入每轮共享的去重池;未声明的工具零开销
    wants_run_state = True

    def __init__(self):
        super().__init__()
        # 懒建客户端:缓存命中或熔断短路时无需走网络(测试可经 _make_client 注入 MockTransport)
        self._client = None

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        """构造源客户端;测试经 transport/ssrf_check 注入 MockTransport 与 SSRF 桩。"""
        raise NotImplementedError

    def _render_lines(self, papers: list[dict]) -> list[str]:
        """把搜索结果渲染成 LLM 可见行(含来源标记;空结果返回空列表)。"""
        raise NotImplementedError

    def _download_url(self, papers: list[dict]) -> str | None:
        """返回要下载的 PDF 地址;None 表示跳过下载。

        默认取首篇结果的 pdf_url——arxiv 预印本恒有,openalex 无开放获取地址时为
        None(用 .get 而不是下标取,避免 KeyError)。
        """
        return papers[0].get("pdf_url") if papers else None

    def execute(self, query: str, max_results: int = 5, download_to: str | None = None,
                year_from: int | None = None, year_to: int | None = None,
                _run_state=None) -> ToolResult:
        # 兜底钳制:模型可能绕过参数 schema 传极端数量(如 1 或 100),越界会让下游
        # reviewer 门禁超预算,故 clamp 到 [3,50] 保证结果规模可控。
        max_results = min(max(3, max_results), 50)
        # 查询缓存:键为(源, query, 年份区间, 钳制后数量),不同源/参数视为不同检索。
        # 命中缓存直接复用,不重复打 API 也不重复入池。
        ckey = (self._source, query, year_from, year_to, max_results)
        cached = query_cache_get(ckey)
        # 缓存命中只短路纯搜索:带下载意图的调用必须重走网络——下游流程是「先搜索、
        # 再门禁、再带 download_to 复调同一 query」,若第二次被缓存短路,PDF 永不落盘。
        if cached is not None and download_to is None:
            return ToolResult(text=f"（缓存）该 query 已搜索过，结果同上；如需不同结果请调整检索词。\n{cached}")
        # 源熔断:连续失败达到阈值时本来源短期短路,提示改用备选源(由 LLM 决策)。
        if breaker_is_open(self._source):
            return ToolResult(text=f"{self._breaker_name} 连续失败已熔断，本次任务请改用 {self._alternate}")
        client = self._client or self._make_client()
        try:
            papers = client.search(query, max_results, year_from, year_to)
            breaker_register_success(self._source)
        except SSRFError as e:
            # SSRF 违规是安全拦截而非源故障,不计数熔断;返回错误让 LLM 调整目标
            return ToolResult(text=f"SSRF blocked: {e}")
        except Exception as e:
            # 网络超时/连接错误等真实源故障 → 记一次失败(熔断计数)
            breaker_register_failure(self._source)
            return ToolResult(text=f"Tool error: {e}")
        if _run_state is not None:
            # 结果入每轮共享去重池(add 内部按 DOI→arXiv ID→规范化标题四级键去重合并,
            # 跨源同论文只留一条)。_run_state 由执行器作为独立 kwarg 传入,不进审计参数。
            _run_state.add(papers)
        lines = self._render_lines(papers)
        # 仅非空结果写缓存:空结果被缓存会让重复 query 返回「（缓存）…」而非「无搜索
        # 结果」,语义不一致。
        if papers:
            query_cache_put(ckey, "\n".join(lines))
        pdf_url = self._download_url(papers)
        if download_to and pdf_url:
            dest = Path(download_to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                _download_pdf(client, pdf_url, dest)
            except Exception as e:
                # 含 SSRF 拦截、重定向未解析完整、响应非 PDF 等情况
                return ToolResult(text=f"下载失败: {e}")
            get_rag_service().index_document(str(dest))   # 写盘后做索引热更新
            lines.append(f"已下载 PDF: {dest}")
        return ToolResult(text="\n".join(lines) if lines else "无搜索结果")
