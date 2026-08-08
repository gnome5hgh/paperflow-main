# tests/test_venue_rank.py
import httpx
import pytest
from paperflow.tools.rank._venue_rank import normalize_venue, lookup_local, passes_q2
from paperflow.tools.rank.lookup_venue_rank import LookupVenueRankTool


@pytest.fixture(autouse=True)
def _no_real_redirect(monkeypatch):
    """跳过 resolve_url_target 的真实网络调用（保持测试封闭）。

    _get 里 resolve_url_target 会发起真实 HEAD 请求并走真实 DNS——
    与"绝不真实网络"约束冲突（且沙箱 DNS 把外网解析到 198.18.0.0/15
    私网段，validate_url_target 会抛 SSRFError，工具会误报"在线查询失败"）。
    桩替换为恒等函数，让 httpx.MockTransport 罐头响应成为唯一网络来源；
    生产环境仍保留真实的逐跳重定向 SSRF 防护（此处仅测试隔离）。
    """
    monkeypatch.setattr("paperflow.tools._http.resolve_url_target", lambda u: u)


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

# 真实 LetPub 数据行格式（2026-08-08 从页面提取）：td[3] 即分区列（4区/1区/2区/3区），
# LetPub 新锐期刊分区表当作中科院 CAS 分区使用。
_LETPUB_HTML = """<html><body>
  <table>
    <tr>
      <td>1234-5678</td><td>Example Journal</td>
      <td>IF: 3.2 h-index: 50</td><td>一区</td>
      <td>大类：计算机科学</td>
    </tr>
  </table>
</body></html>"""

# 阿拉伯数字分区（4区）的真实形态——parser 需把阿拉伯数字转中文（等级值域 cas∈一~四区）
_LETPUB_ARABIC_HTML = """<html><body>
  <table>
    <tr>
      <td>0178-4617</td><td>ALGORITHMICA</td>
      <td>IF: 1 h-index: 67</td><td>4区</td>
      <td>大类：计算机科学</td>
    </tr>
  </table>
</body></html>"""


def test_parse_letpub_real_format():
    """锁定真实 LetPub 数据行格式：td[3] 分区列 + 阿拉伯数字转中文（Fix 1 根因回归）。

    旧正则找 `中科院分区：` / `JCR 分区：` 标记，对真实表格行（td[3]=4区）失配 → 返回
    None → 在线路径永远落到 SJR/未命中。此测试用实测行格式锁死 td[3] 解析。"""
    from paperflow.tools.rank.lookup_venue_rank import _parse_letpub
    assert _parse_letpub(_LETPUB_HTML) == {"ccf": None, "jcr": None, "cas": "一区"}
    assert _parse_letpub(_LETPUB_ARABIC_HTML) == {"ccf": None, "jcr": None, "cas": "四区"}
    assert _parse_letpub("<html>no letpub result</html>") is None


def test_lookup_online_letpub_by_issn():
    from paperflow.tools.rank import lookup_venue_rank as mod
    mod.RANK_CACHE.clear()
    def handler(req):
        assert "example-issn" in str(req.url) or "issn" in str(req.url).lower()
        # Fix 1 根因：URL 必须去掉 &searchfield=all——带上该参数 LetPub 返回 0 数据行
        assert "searchfield" not in str(req.url)
        return httpx.Response(200, text=_LETPUB_HTML)
    tool = LookupVenueRankTool()
    tool._client = LookupVenueRankTool._make_client(
        transport=httpx.MockTransport(handler), ssrf_check=lambda u: None)
    r = tool.execute(venue="Example Journal", issn="example-issn")
    # 断言来源名（_rank_text 里是 "来源：LetPub" 大写形式），而非证据 URL 里的
    # "letpub" 小写片段——后者只是证据链接，来源名才是语义断言
    assert "CAS-一区" in r.text and "来源：LetPub" in r.text


# SJR 搜索页歧义场景：页内列出多本期刊，每本都带档位徽章；整页扫首个 Q 档
# 会命中列表第一本（无关）期刊的 Q4，窗口限定后必须取到目标期刊的 Q2。
_SJR_AMBIGUOUS_HTML = """<html><body>
  <div class="searchlist">
    <table>
      <tr><td class="journaltitle"><a href="/journalsearch.php?p=123">Other Research Journal</a></td>
          <td class="quartile">Q4</td></tr>
      <tr><td class="journaltitle"><a href="/journalsearch.php?p=456">Target Journal Name</a></td>
          <td class="quartile">Q2</td></tr>
    </table>
  </div>
</body></html>"""


def test_lookup_sjr_ambiguous_picks_target_journal_band():
    from paperflow.tools.rank import lookup_venue_rank as mod
    mod.RANK_CACHE.clear()
    def handler(req):
        if "scimagojr" in str(req.url):
            return httpx.Response(200, text=_SJR_AMBIGUOUS_HTML)
        # LetPub 返回无 JCR/CAS 标记的页 → 落到 SJR 兜底
        return httpx.Response(200, text="<html>no letpub result</html>")
    tool = LookupVenueRankTool()
    tool._client = LookupVenueRankTool._make_client(
        transport=httpx.MockTransport(handler), ssrf_check=lambda u: None)
    r = tool.execute(venue="Target Journal Name", issn=None)
    # 整页首个 Q 档是 Other Research Journal 的 Q4；窗口限定后取到目标期刊 Q2
    assert "JCR-Q2" in r.text and "来源：SJR" in r.text


def test_lookup_local_hit_skips_network():
    tool = LookupVenueRankTool()
    r = tool.execute(venue="WWW")
    assert "CCF-A" in r.text and "来源" in r.text


def test_lookup_miss_reports_not_found():
    from paperflow.tools.rank import lookup_venue_rank as mod
    mod.RANK_CACHE.clear()
    def handler(req):
        return httpx.Response(200, text="<html>no results</html>")
    tool = LookupVenueRankTool()
    tool._client = LookupVenueRankTool._make_client(
        transport=httpx.MockTransport(handler), ssrf_check=lambda u: None)
    r = tool.execute(venue="Unknown Venue 404", issn=None)
    assert "未找到等级" in r.text
