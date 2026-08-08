"""共享 SSRF 安全 HTTP 客户端（私有模块，下划线前缀——不进 __init__ 再导出）。

原为 _search_common（拆分自 search.py），更名 _http：本模块是「SSRF 校验 +
httpx 同步客户端」的通用基础设施，不只搜索用——arxiv/openalex 搜索与
lookup_venue_rank 等级查询三处共用同一 SSRF mixin 与 PDF 下载助手
（下载路径缺陷曾暴露于 plan 审查——此处收敛单一实现）。
"""
from pathlib import Path

from paperflow.core.security.network import resolve_url_target


class _HttpClientMixin:
    """共享：SSRF 校验 + httpx 同步客户端（工具已在线程池跑）。

    arxiv/openalex 的搜索 client 与 venue-rank 的抓取 client 都混入此 mixin——
    "Http" 而非 "Search" 指其通用性（rank 查询不是搜索）。
    """

    def _get(self, url: str):
        self.ssrf_check(url)
        url = resolve_url_target(url)          # 重定向逐跳校验（httpx 已是硬依赖）
        self.ssrf_check(url)
        return self.client.get(url)


def _download_pdf(client, url: str, dest: Path) -> None:
    """共享下载助手:逐跳 SSRF 校验重定向链,绝不把 3xx 或非 PDF 响应体写盘。

    重定向链走 resolve_url_target 逐跳校验;arxiv/openalex 两个搜索分支共用此路径,
    避免各自写下载逻辑再引入同类缺陷。
    """
    resolved = resolve_url_target(url)      # HEAD 逐跳 SSRF 校验，返回最终 URL
    client.ssrf_check(resolved)             # 最终 URL 也校验（validate_url_target 要求公网 IP，私网全拒）
    r = client.client.get(resolved, follow_redirects=False)
    if r.is_redirect:
        # HEAD 与 GET 的重定向路径可能分叉（签名/方法相关）；残余 3xx 绝不写盘
        raise RuntimeError(f"重定向未解析完整: {url} -> {resolved}")
    r.raise_for_status()                    # 4xx/5xx
    if not r.content.startswith(b"%PDF"):
        # 服务器 200 但返回 HTML（错误页/登录墙）——magic bytes 比 content-type 可靠
        raise ValueError(f"响应不是 PDF（缺 %PDF magic bytes）: {url}")
    dest.write_bytes(r.content)
