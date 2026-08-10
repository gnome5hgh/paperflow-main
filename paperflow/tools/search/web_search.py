"""WebSearchTool：按 source 指定网站的通用论文搜索工具（arxiv / openalex）。

由原 arxiv_search/openalex_search 双工具合并而来：一次调用只搜一个
网站，source 参数选源，客户端经 _SOURCE_REGISTRY 分发。多源搜索由 searcher 同一轮
并行调用本工具多次承担（复用 multi-call gather + per-run 去重池），本工具不做工具内
多源 fan-out。SSRF 校验与查询缓存/熔断/去重池逻辑与原双工具一致，语义不变。
"""
from paperflow.core.security.network import SSRFError
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.search._common import (
    breaker_is_open,
    breaker_register_failure,
    breaker_register_success,
    query_cache_get,
    query_cache_put,
)
from paperflow.tools.search.clients.arxiv_client import ArxivClient
from paperflow.tools.search.clients.openalex_client import OpenAlexClient

#: source → 客户端类。后续加源：此表加一行 + 客户端类 + parameters.enum 加一个值。
_SOURCE_REGISTRY: dict[str, type] = {"arxiv": ArxivClient, "openalex": OpenAlexClient}


class WebSearchTool(Tool):
    """按 source 搜索论文的通用工具（纯只读，下载走 fetch_pdf）。"""

    name = "web_search"
    description = "搜索论文（按 source 指定网站：arxiv / openalex）。结果自动去重入池。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            "source": {"type": "string", "enum": list(_SOURCE_REGISTRY),
                       "description": "搜索网站来源"},
            # schema 层给 max_results 上下限，让模型端发起调用时就钳在合法区间
            "max_results": {"type": "integer", "default": 5,
                            "minimum": 3, "maximum": 50},
            # 年份是结构化过滤参数，与自由文本 query 平级——不要拼进检索词
            "year_from": {"type": "integer", "description": "起始年份（含），用此参数而非拼进 query"},
            "year_to": {"type": "integer", "description": "结束年份（含），用此参数而非拼进 query"},
        },
        "required": ["query", "source"],
    }
    #: 纯搜索是只读操作（仅出站抓取元数据）——只读会话(low)即可放行;写盘已拆至 fetch_pdf
    risk_level = "low"
    allowed_roots = []
    #: 返回外部内容（标题/摘要/URL），需打"未经安全校验"横幅
    output_scan = "mark"
    side_effects = ["network"]
    #: 声明后执行器会按追踪 ID 注入每轮共享的去重池；未声明的工具零开销
    wants_run_state = True

    def __init__(self):
        super().__init__()
        # 懒建 per-source 客户端：缓存命中或熔断短路时无需走网络
        #（测试可经 _make_client(source, transport=...) 注入 MockTransport）
        self._clients: dict[str, object] = {}

    @classmethod
    def _make_client(cls, source, transport=None, ssrf_check=None):
        """构造 source 对应客户端；测试经 transport/ssrf_check 注入 MockTransport 与 SSRF 桩。"""
        return _SOURCE_REGISTRY[source](transport=transport, ssrf_check=ssrf_check)

    def _get_client(self, source):
        """懒取 source 客户端（缓存命中/熔断短路时不建）。"""
        client = self._clients.get(source)
        if client is None:
            client = self._make_client(source)
            self._clients[source] = client
        return client

    def _render_lines(self, papers: list[dict], source: str) -> list[str]:
        """把搜索结果渲染成 LLM 可见行（来源标记取 source；空结果返回空列表）。"""
        return [f"- [{p['title']}] ({p['year']}) venue={p['venue']} issn={p['issn']} "
                f"pdf={p['pdf_url'] or 'no OA'} 来源={source}" for p in papers]

    def execute(self, query: str, source: str, max_results: int = 5,
                year_from: int | None = None, year_to: int | None = None,
                _run_state=None) -> ToolResult:
        # 未知 source 直接报错并列合法源，不触网——LLM 可据此改传参
        if source not in _SOURCE_REGISTRY:
            return ToolResult(text=f"未知搜索源: {source}，可用: {', '.join(_SOURCE_REGISTRY)}")
        # 兜底钳制:模型可能绕过参数 schema 传极端数量(如 1 或 100)，越界会让下游
        # reviewer 门禁超预算,故 clamp 到 [3,50] 保证结果规模可控。
        max_results = min(max(3, max_results), 50)
        # 查询缓存:键为(源, query, 年份区间, 钳制后数量),不同源/参数视为不同检索。
        # 下载已拆为独立 fetch_pdf 工具，不经过本缓存，无需再为下载意图做缓存旁路。
        ckey = (source, query, year_from, year_to, max_results)
        cached = query_cache_get(ckey)
        if cached is not None:
            return ToolResult(text=f"（缓存）该 query 已搜索过，结果同上；如需不同结果请调整检索词。\n{cached}")
        # 源熔断:连续失败达到阈值时本来源短期短路,提示改用其他源(由 LLM 决策)。
        if breaker_is_open(source):
            return ToolResult(text=f"{source} 连续失败已熔断，本次任务请改用其他 source 或稍后重试")
        client = self._get_client(source)
        try:
            papers = client.search(query, max_results, year_from, year_to)
            breaker_register_success(source)
        except SSRFError as e:
            # SSRF 违规是安全拦截而非源故障,不计数熔断;返回错误让 LLM 调整目标
            return ToolResult(text=f"SSRF blocked: {e}")
        except Exception as e:
            # 网络超时/连接错误等真实源故障 → 记一次失败(熔断计数)
            breaker_register_failure(source)
            return ToolResult(text=f"Tool error: {e}")
        if _run_state is not None:
            # 结果入每轮共享去重池(add 内部按 DOI→arXiv ID→规范化标题四级键去重合并,
            # 跨源同论文只留一条)。_run_state 由执行器作为独立 kwarg 传入,不进审计参数。
            _run_state.add(papers)
        lines = self._render_lines(papers, source)
        # 仅非空结果写缓存:空结果被缓存会让重复 query 返回「（缓存）…」而非「无搜索
        # 结果」,语义不一致。
        if papers:
            query_cache_put(ckey, "\n".join(lines))
        return ToolResult(text="\n".join(lines) if lines else "无搜索结果")
