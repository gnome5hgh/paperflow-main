# paperflow/core/security/network.py
"""
SSRF 防护工具：URL 目标校验与重定向逐跳解析。

``validate_url_target`` 把主机名解析为 IP 后，拒绝回环地址、私网地址、
链路本地地址以及云元数据端点（169.254.169.254、metadata.google.internal），
并支持按 netloc（主机名或 主机:端口）放行本地开发地址；
``resolve_url_target`` 跟随重定向逐跳校验，httpx 为可选依赖——未安装时
原样返回 URL，由调用方决定是否引入 httpx 做重定向防护。

设计要点：
- 本模块不挂载为中间件，由涉及网络请求的工具自行调用；
- DNS 解析采用本机 ``socket.gethostbyname``，以实际解析结果为准，
  避免仅按字面主机名判断造成的绕过（如 0x7f000001 形式的地址）；
- 白名单匹配的是 ``parsed.netloc``（精确的 主机:端口 字符串），
  允许显式放行本地开发地址；
- 白名单与元数据检查的先后关系不对称：``169.254.169.254`` 命中私网分支，
  白名单检查先于元数据检查，因此可被白名单放行；而
  ``metadata.google.internal`` 的检查在白名单之前，不可放行；
- 白名单为精确 netloc 匹配：裸主机条目（如 ``"localhost"``）永不匹配
  带端口的 URL，本地服务须写 ``"127.0.0.1:8070"`` 这样的完整形式。
"""

import ipaddress
import socket
import urllib.parse

PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
]


class SSRFError(Exception):
    """SSRF 防护违规。"""


def validate_url_target(url: str, allowlist: set[str] | None = None) -> None:
    """校验 URL 目标是否允许访问：把主机名解析为 IP，落在私网/元数据区间即抛 SSRFError。

    allowlist 非空时，netloc 命中白名单的本地地址可放行。
    """
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if hostname is None:
        raise SSRFError(f"URL 无 host: {url}")

    if hostname == "metadata.google.internal":
        raise SSRFError("禁止访问 cloud metadata endpoint")

    try:
        host = socket.gethostbyname(hostname)
    except socket.gaierror:
        raise SSRFError(f"无法解析: {hostname}")

    ip = ipaddress.ip_address(host)
    if ip.is_loopback or ip.is_private or any(ip in net for net in PRIVATE_NETS):
        if allowlist and parsed.netloc in allowlist:
            return
        raise SSRFError(f"禁止访问私有地址: {host}")

    if str(ip) == "169.254.169.254":
        raise SSRFError("禁止访问 cloud metadata endpoint")


def resolve_url_target(url: str, allowlist: set[str] | None = None) -> str:
    """跟随重定向逐跳校验，返回最终地址。httpx 为可选依赖——未安装时原样返回。"""
    try:
        import httpx
    except ImportError:
        return url
    validate_url_target(url, allowlist)
    with httpx.Client(follow_redirects=False) as client:
        current = url
        for _ in range(5):
            response = client.head(current, follow_redirects=False)
            if response.is_redirect:
                next_url = str(response.headers.get("location", ""))
                if not next_url:
                    break
                next_url = urllib.parse.urljoin(current, next_url)
                validate_url_target(next_url, allowlist)
                current = next_url
            else:
                break
    return current
