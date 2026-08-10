# tests/test_tools_search.py
import pytest
import httpx
from paperflow.tools import WebSearchTool


@pytest.fixture(autouse=True)
def _no_real_redirect(monkeypatch):
    """跳过 resolve_url_target 的真实网络调用（保持测试封闭）。

    _get 里 resolve_url_target 会发起真实 HEAD 请求并走真实 DNS——
    桩替换为恒等函数，让 httpx.MockTransport 罐头响应成为唯一网络来源；
    生产环境仍保留真实的逐跳重定向 SSRF 防护（此处仅测试隔离）。
    """
    monkeypatch.setattr("paperflow.tools.common._http.resolve_url_target", lambda u: u)


@pytest.fixture(autouse=True)
def _reset_global_search_state():
    """复位模块级 query 缓存/熔断（跨测试共享的全局态）。

    搜索类测试互相污染：前一个测试把某 query 缓存了，后一个测试同一 query
    （含 clamp 后同 max_results）会命中缓存 → 跳过网络/入池等副作用。
    每个测试前清空私有全局态，保证测试顺序无关。
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


def _inject(tool, source, transport, ssrf_check=lambda u: None):
    """把 MockTransport 注入 WebSearchTool 的某 source 客户端。"""
    tool._clients[source] = WebSearchTool._make_client(source, transport=transport,
                                                       ssrf_check=ssrf_check)


def test_web_search_metadata():
    # 返回外部内容（标题/摘要/URL）的搜索工具需 output_scan="mark"；纯只读 low 风险、
    # 仅 network、无写目录；source 为必填 enum；下载已拆为独立 fetch_pdf，无 download_to
    assert WebSearchTool.output_scan == "mark"
    assert WebSearchTool.risk_level == "low"
    assert WebSearchTool.side_effects == ["network"]
    assert WebSearchTool.allowed_roots == []
    props = WebSearchTool.parameters["properties"]
    assert props["source"]["enum"] == ["arxiv", "openalex"]
    assert WebSearchTool.parameters["required"] == ["query", "source"]
    assert "download_to" not in props


def test_web_search_arxiv_parses():
    tool = WebSearchTool()
    _inject(tool, "arxiv", _atransport())
    result = tool.execute(query="link prediction", source="arxiv", max_results=1)
    assert "Graph Neural Networks" in result.text
    assert "来源=arxiv" in result.text


def test_web_search_openalex_parses():
    tool = WebSearchTool()
    _inject(tool, "openalex", _otransport())
    result = tool.execute(query="circRNA", source="openalex", max_results=1)
    assert "circRNA regulation" in result.text
    assert "来源=openalex" in result.text


def test_web_search_invalid_source():
    # 非法 source 不触网，报错并列合法源（LLM 据此改传参）
    tool = WebSearchTool()
    result = tool.execute(query="x", source="unknown")
    assert "未知搜索源" in result.text
    assert "arxiv" in result.text and "openalex" in result.text


def test_ssrf_check_blocks_private():
    # SSRF 拒绝路径：注入抛 SSRFError 的校验桩，断言返回 SSRF 错误而非继续检索
    from paperflow.core.security.network import SSRFError

    def blocking_check(url):
        raise SSRFError("blocked for test")

    tool = WebSearchTool()
    _inject(tool, "arxiv", _atransport(), ssrf_check=blocking_check)
    result = tool.execute(query="x", source="arxiv", max_results=1)
    assert "SSRF" in result.text


def test_arxiv_year_range_in_query():
    # 年份用原生 submittedDate 区间过滤——裸多词 AND submittedDate 会被 arXiv 静默丢弃
    # 日期过滤，必须把裸关键词转 all: 前缀再组合。断言解码后的 URL 含
    # 'all:graph AND all:algorithm AND submittedDate' 与区间边界。
    from urllib.parse import unquote_plus
    seen = {}
    def handler(req):
        seen["q"] = unquote_plus(str(req.url))
        return httpx.Response(200, content=_ATOM.encode(), request=req)
    tool = WebSearchTool()
    _inject(tool, "arxiv", httpx.MockTransport(handler))
    tool.execute(query="graph algorithm", source="arxiv", max_results=3,
                 year_from=2026, year_to=2026)
    assert "all:graph AND all:algorithm AND submittedDate" in seen["q"]
    assert "20260101" in seen["q"] and "20261231" in seen["q"]


def test_max_results_clamped():
    # 低于下限 3 → execute 钳到 3：用 URL 捕获验证 max_results 被钳为 3
    seen = {}
    def handler(req):
        seen["q"] = str(req.url)
        return httpx.Response(200, content=_ATOM.encode(), request=req)
    tool = WebSearchTool()
    _inject(tool, "arxiv", httpx.MockTransport(handler))
    r = tool.execute(query="x", source="arxiv", max_results=1)   # 低于下限 3 → 钳到 3
    assert "Graph Neural Networks" in r.text
    assert "max_results=3" in seen["q"]


def test_search_appends_to_pool_and_dedups():
    # 结果入 per-run 自动去重池；同 arXiv ID 只留一条。第二次调用命中缓存不重复入池
    from paperflow.tools.search._common import SearchRunState
    tool = WebSearchTool()
    _inject(tool, "arxiv", _atransport())
    st = SearchRunState()
    tool.execute(query="link prediction", source="arxiv", max_results=1, _run_state=st)
    tool.execute(query="link prediction", source="arxiv", max_results=1, _run_state=st)
    assert len(st.as_candidates()) == 1


def test_query_cache_hit_returns_marker():
    # 缓存键含 source 前缀：(source, query, year_from, year_to, max_results)
    from paperflow.tools.search._common import SearchRunState, query_cache_put
    query_cache_put(("arxiv", "heterogeneous graph", None, None, 5), "旧结果")
    tool = WebSearchTool()
    _inject(tool, "arxiv", _atransport())
    r = tool.execute(query="heterogeneous graph", source="arxiv", max_results=5,
                     _run_state=SearchRunState())
    assert "（缓存）" in r.text and "旧结果" in r.text


def test_breaker_open_short_circuits():
    # 源连续失败 ≥2 次 → 熔断短路，execute 在打网络前返回提示（fixture 已清状态）
    from paperflow.tools.search._common import (
        breaker_register_failure, breaker_register_success, breaker_is_open,
    )
    breaker_register_success("arxiv")
    breaker_register_failure("arxiv"); breaker_register_failure("arxiv")
    assert breaker_is_open("arxiv")
    tool = WebSearchTool()
    _inject(tool, "arxiv", _atransport())
    r = tool.execute(query="x", source="arxiv", max_results=3)
    assert "熔断" in r.text
    breaker_register_success("arxiv")                      # 清理，防泄漏


def test_empty_result_not_cached():
    # 空结果不入缓存——重复 query 仍返回「无搜索结果」，而非命中缓存返回「（缓存）…」
    empty_atom = ('<?xml version="1.0" encoding="UTF-8"?>'
                  '<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=empty_atom.encode(), request=req))
    tool = WebSearchTool()
    _inject(tool, "arxiv", transport)
    r1 = tool.execute(query="nothing", source="arxiv", max_results=3)
    assert r1.text == "无搜索结果"
    r2 = tool.execute(query="nothing", source="arxiv", max_results=3)
    assert r2.text == "无搜索结果"
