# tests/test_venue_rank.py
import httpx
import pytest
from paperflow.tools._venue_rank import normalize_venue, lookup_local, passes_q2
from paperflow.tools.lookup_venue_rank import LookupVenueRankTool


@pytest.fixture(autouse=True)
def _no_real_redirect(monkeypatch):
    """跳过 resolve_url_target 的真实网络调用（保持测试封闭）。

    _get 里 resolve_url_target 会发起真实 HEAD 请求并走真实 DNS——
    与"绝不真实网络"约束冲突（且沙箱 DNS 把外网解析到 198.18.0.0/15
    私网段，validate_url_target 会抛 SSRFError，工具会误报"在线查询失败"）。
    桩替换为恒等函数，让 httpx.MockTransport 罐头响应成为唯一网络来源；
    生产环境仍保留真实的逐跳重定向 SSRF 防护（此处仅测试隔离）。
    """
    monkeypatch.setattr("paperflow.tools._search_common.resolve_url_target", lambda u: u)


def test_normalize_venue():
    assert normalize_venue("Proceedings of the ACM Web Conference") == "www"
    assert normalize_venue("  NeurIPS ") == "neurips"
    assert normalize_venue("IEEE Transactions on Knowledge and Data Engineering") == "ieeetransactionsonknowledgeanddataengineering"


def test_local_map_known_venues():
    assert lookup_local("WWW")["ccf"] == "A"
    assert lookup_local("KDD")["ccf"] == "A"
    assert lookup_local("NeurIPS")["ccf"] == "A"
    assert lookup_local("ICML")["ccf"] == "A"
    assert lookup_local("AAAI")["ccf"] == "B"
    assert lookup_local("TPAMI")["jcr"] == "Q1"


def test_passes_q2_equivalence_table_B():
    assert passes_q2({"ccf": "A", "jcr": None, "cas": None})
    assert passes_q2({"ccf": "B", "jcr": None, "cas": None})
    assert not passes_q2({"ccf": "C", "jcr": None, "cas": None})
    assert passes_q2({"ccf": None, "jcr": "Q1", "cas": None})
    assert passes_q2({"ccf": None, "jcr": "Q2", "cas": None})
    assert not passes_q2({"ccf": None, "jcr": "Q3", "cas": None})
    assert passes_q2({"ccf": None, "jcr": None, "cas": "一区"})
    assert not passes_q2({"ccf": None, "jcr": None, "cas": "三区"})
    assert not passes_q2({"ccf": None, "jcr": None, "cas": None})

_LETPUB_HTML = """<html><body>
  <div class="journal-info">Journal Name: Example Journal</div>
  <div class="journal-rank">JCR 分区：Q1<br/>中科院分区：一区</div>
</body></html>"""


def test_lookup_online_letpub_by_issn():
    from paperflow.tools import lookup_venue_rank as mod
    mod.RANK_CACHE.clear()
    def handler(req):
        assert "example-issn" in str(req.url) or "issn" in str(req.url).lower()
        return httpx.Response(200, text=_LETPUB_HTML)
    tool = LookupVenueRankTool()
    tool._client = LookupVenueRankTool._make_client(
        transport=httpx.MockTransport(handler), ssrf_check=lambda u: None)
    r = tool.execute(venue="Example Journal", issn="example-issn")
    assert "Q1" in r.text and "一区" in r.text and "letpub" in r.text


def test_lookup_local_hit_skips_network():
    tool = LookupVenueRankTool()
    r = tool.execute(venue="WWW")
    assert "CCF-A" in r.text and "来源" in r.text


def test_lookup_miss_reports_not_found():
    from paperflow.tools import lookup_venue_rank as mod
    mod.RANK_CACHE.clear()
    def handler(req):
        return httpx.Response(200, text="<html>no results</html>")
    tool = LookupVenueRankTool()
    tool._client = LookupVenueRankTool._make_client(
        transport=httpx.MockTransport(handler), ssrf_check=lambda u: None)
    r = tool.execute(venue="Unknown Venue 404", issn=None)
    assert "未找到等级" in r.text
