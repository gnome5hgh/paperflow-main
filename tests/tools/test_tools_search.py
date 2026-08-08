# tests/test_tools_search.py
import pytest
import httpx
from paperflow.tools import ArxivSearchTool, OpenAlexSearchTool


@pytest.fixture(autouse=True)
def _no_real_redirect(monkeypatch):
    """跳过 resolve_url_target 的真实网络调用（保持测试封闭）。

    _get 里 resolve_url_target 会发起真实 HEAD 请求并走真实 DNS——
    与"绝不真实网络"约束冲突（且沙箱 DNS 把外网解析到 198.18.0.0/15
    私网段，validate_url_target 会抛 SSRFError）。桩替换为恒等函数，
    让 httpx.MockTransport 罐头响应成为唯一网络来源；
    生产环境仍保留真实的逐跳重定向 SSRF 防护（此处仅测试隔离）。
    """
    monkeypatch.setattr("paperflow.tools._http.resolve_url_target", lambda u: u)


@pytest.fixture(autouse=True)
def _reset_global_search_state():
    """复位模块级 query 缓存/熔断（A4/A5 是跨测试共享的全局态）。

    Task 3 引入 A4 缓存后，搜索类测试互相污染：前一个测试把某 query 缓存了，
    后一个测试同一 query（含 clamp 后同 max_results）会命中缓存 → 跳过网络/入池
    等副作用。例如 test_arxiv_search_parses 缓存了 ("arxiv","link prediction",...,
    3)，同 query 的后续测试会缓存命中而不再走真实网络。
    每个测试前清空私有全局态，保证测试顺序无关（私有符号仅测试复位用）。
    """
    from paperflow.tools.search import _common as ss
    ss._QUERY_CACHE.clear()
    ss._SOURCE_BREAKER.clear()
    yield

_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Graph Neural Networks for Link Prediction</title>
    <id>http://arxiv.org/abs/2101.00001v1</id>
    <published>2021-01-01T00:00:00Z</published>
    <summary>Abstract text here.</summary>
    <author><name>Alice</name></author>
  </entry>
</feed>"""

_OPENALEX = {
    "results": [{
        "id": "https://openalex.org/W0000000001",
        "display_name": "circRNA regulation",
        "publication_year": 2022,
        "cited_by_count": 30,
        "abstract_inverted_index": {"circRNA": [0], "regulates": [1]},
        "authorships": [{"author": {"display_name": "Bob"}}],
        "best_oa_location": {"pdf_url": "https://example.com/paper.pdf"},
    }]
}


def _atransport():
    return httpx.MockTransport(lambda req: httpx.Response(200, content=_ATOM.encode()))


def _otransport():
    return httpx.MockTransport(lambda req: httpx.Response(200, json=_OPENALEX))


def test_search_tools_metadata():
    # MINOR-7：返回外部内容（标题/摘要/URL）的搜索工具需 output_scan="mark"，
    # 与 ReadFileTool/ReadPdfTool 一致（外部内容打"未经安全校验"横幅）。
    assert ArxivSearchTool.output_scan == "mark"
    assert OpenAlexSearchTool.output_scan == "mark"
    # 下载拆为独立 fetch_pdf 后，搜索工具是纯只读：low 风险、仅 network、无写目录
    assert ArxivSearchTool.risk_level == "low"
    assert OpenAlexSearchTool.risk_level == "low"
    assert ArxivSearchTool.side_effects == ["network"]
    assert OpenAlexSearchTool.side_effects == ["network"]
    assert ArxivSearchTool.allowed_roots == []
    assert OpenAlexSearchTool.allowed_roots == []
    # 不再带 download_to 参数（下载走 fetch_pdf 工具）
    assert "download_to" not in ArxivSearchTool.parameters["properties"]
    assert "download_to" not in OpenAlexSearchTool.parameters["properties"]


def test_arxiv_search_parses():
    tool = ArxivSearchTool()
    tool._client = ArxivSearchTool._make_client(transport=_atransport(), ssrf_check=lambda u: None)
    result = tool.execute(query="link prediction", max_results=1)
    assert "Graph Neural Networks" in result.text


def test_openalex_search_parses():
    tool = OpenAlexSearchTool()
    tool._client = OpenAlexSearchTool._make_client(transport=_otransport(), ssrf_check=lambda u: None)
    result = tool.execute(query="circRNA", max_results=1)
    assert "circRNA regulation" in result.text


def test_ssrf_check_blocks_private():
    # SSRF 拒绝路径：注入抛 SSRFError 的校验桩，断言工具返回 SSRF 错误而非继续检索
    from paperflow.core.security.network import SSRFError

    def blocking_check(url):
        raise SSRFError("blocked for test")

    tool = ArxivSearchTool()
    tool._client = ArxivSearchTool._make_client(transport=_atransport(), ssrf_check=blocking_check)
    result = tool.execute(query="x", max_results=1)
    assert "SSRF" in result.text


# ── Task 3：A1 年份结构化过滤 / B2 钳制 / A3 池 append / A4 缓存 / A5 熔断 ──

def test_arxiv_year_range_in_query():
    # A1 + Fix 3：年份用原生 submittedDate 区间过滤——但裸多词 AND submittedDate 会被
    # arXiv 静默丢弃日期过滤（2026-08-08 实测返回旧论文），必须把裸关键词转 all: 前缀
    # 再组合。断言解码后的 URL 含 'all:graph AND all:algorithm AND submittedDate'
    #（而非裸词 'graph AND algorithm'）。URL 是 urlencode 编码的，先 unquote_plus 还原。
    from urllib.parse import unquote_plus
    from paperflow.tools.search.arxiv_search import ArxivClient
    seen = {}
    def handler(req):
        seen["q"] = unquote_plus(str(req.url))
        return httpx.Response(200, content=_ATOM.encode(), request=req)
    client = ArxivClient(transport=httpx.MockTransport(handler), ssrf_check=lambda u: None)
    client.search("graph algorithm", max_results=3, year_from=2026, year_to=2026)
    assert "all:graph AND all:algorithm AND submittedDate" in seen["q"]
    assert "20260101" in seen["q"] and "20261231" in seen["q"]   # 区间边界仍保留


def test_max_results_clamped():
    # B2：低于下限 3 → execute 钳到 3——用 URL 捕获验证 max_results 被钳为 3
    # （review finding 3：mock 恒返回固定 1 篇，单靠文本断言无法证明钳制生效）
    from paperflow.tools.search.arxiv_search import ArxivClient
    seen = {}
    def handler(req):
        seen["q"] = str(req.url)
        return httpx.Response(200, content=_ATOM.encode(), request=req)
    tool = ArxivSearchTool()
    tool._client = ArxivSearchTool._make_client(transport=httpx.MockTransport(handler), ssrf_check=lambda u: None)
    r = tool.execute(query="x", max_results=1)   # 低于下限 3 → 钳到 3
    assert "Graph Neural Networks" in r.text
    assert "max_results=3" in seen["q"]          # 请求 URL 里 max_results 已被钳为 3


def test_search_appends_to_pool_and_dedups():
    # A3：结果入 per-run 自动去重池；同 arXiv ID 只留一条。第二次调用命中 A4 缓存
    # 不重复入池——"池去重 + 缓存去重"两条路径合起来保证池内唯一。
    from paperflow.tools.search._common import SearchRunState
    tool = ArxivSearchTool()
    tool._client = ArxivSearchTool._make_client(transport=_atransport(), ssrf_check=lambda u: None)
    st = SearchRunState()
    tool.execute(query="link prediction", max_results=1, _run_state=st)
    tool.execute(query="link prediction", max_results=1, _run_state=st)
    assert len(st.as_candidates()) == 1          # 同 arXiv ID 只留一条


def test_query_cache_hit_returns_marker():
    # A4：缓存命中返回"（缓存）"标记 + 旧结果。缓存键含源前缀 "arxiv"
    # （与 execute 内 ckey 一致：(源, query, year_from, year_to, max_results)）
    from paperflow.tools.search._common import SearchRunState, query_cache_get, query_cache_put
    key = ("arxiv", "heterogeneous graph", None, None, 5)
    query_cache_put(key, "旧结果")
    tool = ArxivSearchTool()
    tool._client = ArxivSearchTool._make_client(transport=_atransport(), ssrf_check=lambda u: None)
    r = tool.execute(query="heterogeneous graph", max_results=5, _run_state=SearchRunState())
    assert "（缓存）" in r.text and "旧结果" in r.text


def test_breaker_open_short_circuits():
    # A5：源连续失败 ≥2 次 → 熔断短路，execute 在打网络前返回提示（fixture 已清状态）
    from paperflow.tools.search._common import breaker_register_failure, breaker_register_success, breaker_is_open
    breaker_register_success("arxiv")                      # 复位
    breaker_register_failure("arxiv"); breaker_register_failure("arxiv")
    assert breaker_is_open("arxiv")
    tool = ArxivSearchTool()
    tool._client = ArxivSearchTool._make_client(transport=_atransport(), ssrf_check=lambda u: None)
    r = tool.execute(query="x", max_results=3)
    assert "熔断" in r.text
    breaker_register_success("arxiv")                      # 清理，防泄漏


# ── Task 3 review 修复的回归测试（finding 4）──

def test_empty_result_not_cached():
    # review finding 4：空结果不入缓存——重复 query 仍返回「无搜索结果」，
    # 而不是命中缓存返回「（缓存）…」（空缓存条目会让语义不一致）。
    empty_atom = ('<?xml version="1.0" encoding="UTF-8"?>'
                  '<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=empty_atom.encode(), request=req))
    tool = ArxivSearchTool()
    tool._client = ArxivSearchTool._make_client(transport=transport, ssrf_check=lambda u: None)
    r1 = tool.execute(query="nothing", max_results=3)
    assert r1.text == "无搜索结果"
    r2 = tool.execute(query="nothing", max_results=3)   # 未入缓存 → 再次真实搜索
    assert r2.text == "无搜索结果"                        # 而非「（缓存）…」
