"""共享 SSRF 安全 HTTP 客户端（私有模块，下划线前缀——不进 __init__ 再导出）。

本模块是「SSRF 校验 + httpx 同步客户端」的通用基础设施，不只搜索用——arxiv/openalex
搜索与 lookup_venue_rank 等级查询共用同一 SSRF mixin。PDF 下载的抓取与 SSRF 校验见
FetchPdfTool（paperflow/tools/search/fetch_pdf.py）。
"""
from paperflow.core.security.network import resolve_url_target


class _HttpClientMixin:
    """共享：SSRF 校验 + httpx 同步客户端（工具已在线程池跑）。

    arxiv/openalex 的搜索 client 与 venue-rank 的抓取 client 都混入此 mixin——
    "Http" 而非 "Search" 指其通用性（rank 查询不是搜索）。
    """

    def _get(self, url: str):
        """带 SSRF 逐跳校验的 GET 请求,返回 httpx 响应对象。

        重定向前后各校验一次:先查原始 URL,再用 resolve_url_target 把重定向链逐跳
        解析并校验,最终 URL 再校验一次——防 3xx 跳转到私网/内网地址。
        """
        self.ssrf_check(url)
        url = resolve_url_target(url)          # 重定向逐跳校验（httpx 已是硬依赖）
        self.ssrf_check(url)
        return self.client.get(url)
