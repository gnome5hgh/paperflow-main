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

#: 私有/保留 IP 网络列表（RFC 1918、环回、链路本地）
#: 用于判断解析后的 IP 是否属于内部网络，禁止访问这些地址以防止 SSRF 攻击。
PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
]


class SSRFError(Exception):
    """SSRF 防护违规。

    当目标 URL 解析到私网地址、元数据端点或解析失败时抛出此异常。
    """


def validate_url_target(url: str, allowlist: set[str] | None = None) -> None:
    """校验 URL 目标是否允许访问：把主机名解析为 IP，落在私网/元数据区间即抛 SSRFError。

    allowlist 非空时，netloc 命中白名单的本地地址可放行。

    Args:
        url: 待校验的 URL 字符串（如 "http://internal-server.local/health"）
        allowlist: 白名单集合，元素为精确的 netloc 字符串（如 "127.0.0.1:8070"）。
                   裸主机名（如 "localhost"）不会匹配带端口的 URL。

    Raises:
        SSRFError: 发生以下情况时抛出：
            - URL 解析后无 hostname
            - 主机名无法通过 DNS 解析
            - 解析后的 IP 落在私有网络范围内（且不在白名单中）
            - 主机名为 metadata.google.internal 或 IP 为 169.254.169.254

    算法思路：
        1. 解析 URL 提取 hostname。
        2. 特殊检查 metadata.google.internal（云元数据端点，必须严格拦截）。
        3. 调用 socket.gethostbyname 将主机名解析为 IP（防止 0x7f000001 等绕过形式）。
        4. 判断 IP 是否属于私有网络（环回、RFC 1918、链路本地）。
        5. 如果命中私网，检查白名单（精确匹配 parsed.netloc）；在则放行，否则拦截。
        6. 明确检查 169.254.169.254（AWS/通用元数据 IP）并拦截。

    注意：169.254.169.254 虽在 PRIVATE_NETS 范围内，但单独检查是为了：
        - 增强可读性（明确拒绝云元数据）
        - 如果将来 PRIVATE_NETS 变更，仍有双重保障
    """
    # ---- 1. 解析 URL ----
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if hostname is None:
        raise SSRFError(f"URL 无 host: {url}")

    # ---- 2. 云元数据端点硬拦截（白名单无法放行） ----
    # 该检查在白名单之前执行，意味着即使用户将 "metadata.google.internal" 加入白名单，
    # 也会被拦截。这是有意设计——云元数据服务是最高敏感目标，不应有任何例外。
    if hostname == "metadata.google.internal":
        raise SSRFError("禁止访问 cloud metadata endpoint")

    # ---- 3. DNS 解析 ----
    # 使用本机 DNS 解析，将主机名转为实际 IP。
    # 这一步至关重要：攻击者可能使用 0x7f000001（十进制 IP）、
    # 0x7f.0.0.1（混合进制）或其他非标准表示法来绕过基于字符串的检查。
    # socket.gethostbyname 会将这些全部规范化为标准点分十进制 IP。
    try:
        host = socket.gethostbyname(hostname)
    except socket.gaierror:
        raise SSRFError(f"无法解析: {hostname}")

    ip = ipaddress.ip_address(host)

    # ---- 4. 私有/保留 IP 范围检查 ----
    # 检查解析出的 IP 是否落在环回、私有或链路本地地址范围内。
    # ip.is_private 在 Python 3.10+ 中已包含 RFC 1918 和环回，但为了完整性和
    # 对链路本地（169.254.0.0/16）的显式覆盖，仍保留了对 PRIVATE_NETS 的遍历。
    if ip.is_loopback or ip.is_private or any(ip in net for net in PRIVATE_NETS):
        # 白名单精确匹配解析后的 netloc（主机名:端口）。
        # 注意：如果用户添加了 "localhost:8080"，但 parsed.netloc 是 "127.0.0.1:8080"，
        # 则不会匹配。这要求白名单必须包含实际的 IP 或与原始 URL 完全一致的 host:port。
        if allowlist and parsed.netloc in allowlist:
            return # 白名单放行
        raise SSRFError(f"禁止访问私有地址: {host}")

    # ---- 5. 元数据 IP 硬拦截（兜底） ----
    # 尽管 169.254.0.0/16 已在上面的 PRIVATE_NETS 中被拦截，但此处单独显式检查
    # 可作为防御纵深，确保即使上面的网络列表被误修改，依然能拦截云元数据请求。
    if str(ip) == "169.254.169.254":
        raise SSRFError("禁止访问 cloud metadata endpoint")


def resolve_url_target(url: str, allowlist: set[str] | None = None) -> str:
    """跟随重定向逐跳校验，返回最终地址。httpx 为可选依赖——未安装时原样返回。

    设计目标：防止攻击者利用 HTTP 重定向将请求从外部公网地址跳转到内部私有地址，
    从而绕过首次请求时的 URL 校验。

    Args:
        url: 起始请求 URL
        allowlist: 白名单（透传给 validate_url_target）

    Returns:
        str: 经过所有重定向后的最终 URL；若未安装 httpx，则原样返回输入 URL。

    算法思路：
        1. 如果未安装 httpx，降级返回原 URL（不提供重定向防护）。
        2. 首次校验起始 URL（validate_url_target）。
        3. 使用 httpx.Client 发起 HEAD 请求（不自动跟随重定向）。
        4. 如果响应是重定向（3xx），从 Location 头提取目标地址。
           - 使用 urljoin 处理相对路径重定向。
           - 对新目标地址执行 validate_url_target（重定向目标也必须通过 SSRF 校验）。
        5. 重复步骤 3-4，最多 5 跳（防止无限重定向循环）。
        6. 返回最终 URL。

    为什么最多 5 跳：HTTP 标准建议客户端最多跟随 5 次重定向，
    防止攻击者构造过长的重定向链消耗资源或绕过检测。
    """
    try:
        import httpx
    except ImportError:
        # httpx 未安装：无法进行重定向校验，安全降级。
        # 调用方应自行评估风险或安装 httpx。
        return url

    # 首次校验原始 URL
    validate_url_target(url, allowlist)

    # 创建不自动跟随重定向的 Client，手动控制跳转流程
    with httpx.Client(follow_redirects=False) as client:
        current = url
        # 最多允许 5 次重定向，防止无限循环
        for _ in range(5):
            # 使用 HEAD 请求只获取响应头，不下载响应体（更高效，且避免潜在危险内容）
            response = client.head(current, follow_redirects=False)
            if response.is_redirect:
                next_url = str(response.headers.get("location", ""))
                if not next_url:
                    break # Location 头为空，终止

                # 处理相对路径重定向（如 Location: /new-path）
                next_url = urllib.parse.urljoin(current, next_url)

                # 关键步骤：对重定向目标重新进行 SSRF 校验
                # 这可以阻止攻击者先访问合法外部域名，再重定向到 127.0.0.1
                validate_url_target(next_url, allowlist)

                # 更新当前 URL，进入下一轮循环
                current = next_url
            else:
                # 非重定向响应（2xx、4xx、5xx），停止跟随
                break

    return current
