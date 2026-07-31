# paperflow/core/security/network.py
"""
SSRF 防护工具：URL 目标校验与重定向逐跳解析。

``validate_url_target`` 将 hostname 解析为 IP 后拒绝 loopback / 私网 /
link-local 地址与 cloud metadata endpoint（169.254.169.254、
metadata.google.internal），支持按 netloc（host 或 host:port）放行；
``resolve_url_target`` 跟随重定向逐跳校验，httpx 为可选依赖——
未安装时原样返回 URL，由网络 Tool 层决定是否引入 httpx 做重定向防护。

设计要点：
- 本模块不挂载为中间件，由 Layer 2/3 网络 Tool 自行调用；
- DNS 解析采用本机 ``socket.gethostbyname``，以实际解析结果为准，
  避免仅按字面 hostname 判断造成的绕过（如 0x7f000001 形式的地址）；
- allowlist 匹配的是 ``parsed.netloc``（精确的 host:port 字符串），
  允许显式放行本地开发地址；
- allowlist 不对称：``169.254.169.254`` 命中的是 PRIVATE_NETS 分支，
  allowlist 检查先于 metadata 检查 → 可被 allowlist 放行；
  而 ``metadata.google.internal`` 的检查在 allowlist 之前 → 不可放行；
- allowlist 为精确 netloc 匹配：裸 host 条目（如 ``"localhost"``）
  永不匹配带端口的 URL，GROBID 本地服务场景须写 ``"127.0.0.1:8070"``。
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
    """跟随重定向逐跳校验。httpx 为可选依赖——未安装时原样返回。"""
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
