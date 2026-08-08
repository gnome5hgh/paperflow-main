# tests/tools/test_tools_fetch_pdf.py
"""FetchPdfTool 下载工具测试：重定向解析、SSRF 拦截、非 PDF 拒绝、索引热更新。

下载职责自 arxiv/openalex 搜索工具拆出后的独立测试（原 test_tools_search.py
的下载用例迁至此，改为直接驱动 FetchPdfTool）。
"""
import httpx
import pytest

from paperflow.tools.search.fetch_pdf import FetchPdfTool


@pytest.fixture(autouse=True)
def _no_real_redirect(monkeypatch):
    """桩掉 fetch_pdf 模块的 resolve_url_target 真实网络调用（保持测试封闭）。

    生产环境仍保留真实的逐跳重定向 SSRF 防护；此处让 httpx.MockTransport
    罐头响应成为唯一网络来源（同搜索工具测试的隔离约定）。
    """
    monkeypatch.setattr("paperflow.tools.search.fetch_pdf.resolve_url_target", lambda u: u)


def test_fetch_pdf_metadata():
    # 下载是写操作 → medium 风险 + 写盘副作用 + pdf 根；返回本地路径，无外部内容
    assert FetchPdfTool.risk_level == "medium"
    assert FetchPdfTool.allowed_roots == ["pdf"]
    assert FetchPdfTool.side_effects == ["network", "write_file"]
    assert FetchPdfTool.output_scan is None
    props = FetchPdfTool.parameters["properties"]
    assert FetchPdfTool.parameters["required"] == ["url", "download_to"]
    assert props["url"]["format"] == "url"
    assert props["download_to"]["format"] == "path"


def test_fetch_resolves_redirect_and_indexes(tmp_path, monkeypatch):
    # 重定向 → 200 PDF 开心路径：解析后的最终 URL 被 GET、写盘字节正确、触发 index_document
    import paperflow.tools.search.fetch_pdf as fetch_mod

    def fake_resolve(url):
        # 模拟 HEAD 逐跳校验把 url 解析到最终 PDF 地址
        return "http://test/real.pdf"
    monkeypatch.setattr(fetch_mod, "resolve_url_target", fake_resolve)

    got = []
    def handler(req):
        got.append(str(req.url))
        return httpx.Response(200, content=b"%PDF-1.4 fake content", request=req)
    transport = httpx.MockTransport(handler)
    tool = FetchPdfTool()
    tool._client = FetchPdfTool._make_client(transport=transport, ssrf_check=lambda u: None)

    class FakeSvc:
        def __init__(self):
            self.indexed = []
        def index_document(self, p):
            self.indexed.append(p)
    fake = FakeSvc()
    monkeypatch.setattr("paperflow.tools.search.fetch_pdf.get_rag_service", lambda: fake)

    dest = tmp_path / "out.pdf"
    result = tool.execute(url="http://arxiv.org/pdf/2101.00001", download_to=str(dest))
    assert "已下载" in result.text
    assert dest.read_bytes().startswith(b"%PDF")          # 写盘字节正确
    assert fake.indexed == [str(dest)]                    # 触发 index_document
    assert "http://test/real.pdf" in got                  # GET 的是解析后的最终 URL


def test_fetch_blocks_private_redirect(tmp_path, monkeypatch):
    # 重定向 → 私网 IP 被拒：下载报错、无文件落盘、不触发索引
    from paperflow.core.security.network import SSRFError
    import paperflow.tools.search.fetch_pdf as fetch_mod

    monkeypatch.setattr(fetch_mod, "resolve_url_target",
                        lambda url: "http://169.254.169.254/latest/meta-data/")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"%PDF", request=req))
    tool = FetchPdfTool()

    def check(url):
        if "169.254.169.254" in url:
            raise SSRFError("blocked for test")
    tool._client = FetchPdfTool._make_client(transport=transport, ssrf_check=check)

    class FakeSvc:
        def __init__(self):
            self.indexed = []
        def index_document(self, p):
            self.indexed.append(p)
    fake = FakeSvc()
    monkeypatch.setattr("paperflow.tools.search.fetch_pdf.get_rag_service", lambda: fake)

    dest = tmp_path / "evil.pdf"
    result = tool.execute(url="http://example.com/paper.pdf", download_to=str(dest))
    assert "下载失败" in result.text or "SSRF" in result.text
    assert not dest.exists()                              # 无文件落盘
    assert fake.indexed == []                             # 不触发索引


def test_fetch_rejects_non_pdf(tmp_path, monkeypatch):
    # 服务器 200 但返回 HTML（登录墙/错误页）→ magic bytes 校验拒绝写盘
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"<html>login required</html>", request=req))
    tool = FetchPdfTool()
    tool._client = FetchPdfTool._make_client(transport=transport, ssrf_check=lambda u: None)

    dest = tmp_path / "x.pdf"
    result = tool.execute(url="http://example.com/paper", download_to=str(dest))
    assert "下载失败" in result.text
    assert not dest.exists()
