# tests/test_tools_search.py
import pytest
import httpx
from paperflow.tools.search import ArxivSearchTool, OpenAlexSearchTool


@pytest.fixture(autouse=True)
def _no_real_redirect(monkeypatch):
    """跳过 resolve_url_target 的真实网络调用（保持测试封闭）。

    _get 里 resolve_url_target 会发起真实 HEAD 请求并走真实 DNS——
    与"绝不真实网络"约束冲突（且沙箱 DNS 把外网解析到 198.18.0.0/15
    私网段，validate_url_target 会抛 SSRFError）。桩替换为恒等函数，
    让 httpx.MockTransport 罐头响应成为唯一网络来源；
    生产环境仍保留真实的逐跳重定向 SSRF 防护（此处仅测试隔离）。
    """
    monkeypatch.setattr("paperflow.tools.search.resolve_url_target", lambda u: u)

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
