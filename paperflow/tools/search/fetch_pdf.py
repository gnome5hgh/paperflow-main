"""FetchPdfTool：从搜索结果 URL 下载 PDF 到本地资料库（独立下载工具）。

下载职责独立成工具，让搜索工具保持纯只读：写盘副作用集中在本工具，审计日志里
「写盘」动作归于 fetch_pdf，不藏在名为 search 的工具下。SSRF 校验与搜索客户端
共用同一套（见 paperflow/tools/common/_http.py），本工具在 _fetch 里做逐跳重定向
校验 + %PDF magic bytes 校验，绝不把非 PDF 响应体写盘。
"""
import httpx
from pathlib import Path
from urllib.parse import urljoin

from paperflow.core.security.network import validate_url_target
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service


class FetchPdfTool(Tool):
    """下载 PDF 工具：带 SSRF 校验的网络抓取 + 写盘 + 索引热更新。"""

    name = "fetch_pdf"
    # description 与行为对齐:纯下载,url 取搜索结果行的 pdf= 字段(LLM 据此传参)
    description = "下载 PDF 到本地资料库（SSRF 校验 + 写盘后索引热更新）。url 取搜索结果行的 pdf= 字段。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "url",
                    "description": "PDF 下载地址（来自搜索结果的 pdf 字段）"},
            "download_to": {"type": "string", "format": "path",
                            "description": "PDF 保存绝对路径（vault pdf 根内）"},
        },
        "required": ["url", "download_to"],
    }
    #: 下载是写操作——只读会话(风险上限 low)不应触碰本地资料库
    risk_level = "medium"
    allowed_roots = ["pdf"]
    side_effects = ["network", "write_file"]
    #: 返回本地路径与状态，无外部内容——不需打 mark 横幅
    output_scan = None

    def __init__(self):
        super().__init__()
        # 懒建 (httpx.Client, ssrf_check)：缓存命中或短路时无需走网络，
        # 测试经 _make_client 注入 MockTransport 与 SSRF 桩
        self._client = None

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        """构造下载客户端；测试经 transport/ssrf_check 注入 MockTransport 与 SSRF 桩。"""
        return httpx.Client(transport=transport, timeout=30.0), (ssrf_check or validate_url_target)

    def _fetch(self, client, ssrf_check, url: str, dest: Path) -> None:
        """真实 GET 上逐跳跟随重定向，每跳做 SSRF 校验，绝不把 3xx 或非 PDF 响应体写盘。

        背景（真实使用测试 P1-2）：此前先以 HEAD 预解析重定向链、再对最终 URL 做
        禁跟随的 GET——Springer/DOI 直链的 HEAD 与 GET 重定向路径分叉（cookie/URL
        编码差异），HEAD 预解析结果对 GET 无效，「重定向未解析完整」100% 失败。
        现改为在真实 GET 上逐跳跟随：每一跳的目标 URL 都过 ssrf_check（校验的是
        真实请求链，安全性不弱于 HEAD 方案），最多 5 跳防循环。响应缺 %PDF magic
        bytes（服务器 200 但返回 HTML/登录墙）一律抛错，宁可失败也不写脏数据。
        """
        ssrf_check(url)                         # 起始 URL 校验（validate_url_target 要求公网 IP）
        current = url
        for _ in range(5):
            r = client.get(current, follow_redirects=False)
            if not r.is_redirect:
                break
            location = r.headers.get("location", "")
            if not location:
                raise RuntimeError(f"重定向缺 Location 头: {current}")
            current = urljoin(current, location)
            ssrf_check(current)                 # 每一跳都校验（防先跳合法站再跳私网）
        else:
            raise RuntimeError(f"重定向超过 5 跳: {url}")
        r.raise_for_status()                    # 4xx/5xx
        if not r.content.startswith(b"%PDF"):
            raise ValueError(f"响应不是 PDF（缺 %PDF magic bytes）: {url}")
        dest.write_bytes(r.content)

    def execute(self, url: str, download_to: str) -> ToolResult:
        """下载 PDF 到本地并触发索引热更新；失败返回可行动报错文本。"""
        client, ssrf_check = self._client or self._make_client()
        dest = Path(download_to)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fetch(client, ssrf_check, url, dest)
        except Exception as e:
            # 含 SSRF 拦截、重定向未解析完整、响应非 PDF 等情况
            return ToolResult(text=f"下载失败: {e}")
        note = ""
        try:
            get_rag_service().index_document(str(dest))   # 写盘后做索引热更新
        except Exception as e:
            # Milvus 是外部服务可能未启动：索引失败只降级为提示，不掩盖下载成功
            note = f"（索引失败：{e}）"
        return ToolResult(text=f"已下载 PDF: {dest}{note}")
