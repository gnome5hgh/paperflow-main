"""FetchPdfTool：从搜索结果 URL 下载 PDF 到本地资料库（独立下载工具）。

从 arxiv/openalex 搜索工具里拆出的下载职责（原 BaseSearchTool.execute 的
download_to 分支）。拆出后：

- 搜索工具降为纯只读（low 风险），写盘副作用集中在本工具；
- 审计日志里「写盘」动作归于 fetch_pdf，不再藏在名为 search 的工具下。

SSRF 校验逻辑从 paperflow/tools/common/_http.py 的 _download_pdf 助手原样并入本工具的
_fetch 方法——该助手仅被下载路径使用，拆分后无跨模块复用方，故不另留模块函数。
"""
import httpx
from pathlib import Path

from paperflow.core.security.network import resolve_url_target, validate_url_target
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service


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
        """逐跳 SSRF 校验重定向链，绝不把 3xx 或非 PDF 响应体写盘。

        重定向链走 resolve_url_target 逐跳校验；残余 3xx（HEAD 与 GET 的重定向路径
        可能分叉）或响应缺 %PDF magic bytes（服务器 200 但返回 HTML/登录墙）一律
        抛错，宁可失败也不写脏数据。
        """
        resolved = resolve_url_target(url)      # HEAD 逐跳 SSRF 校验，返回最终 URL
        ssrf_check(resolved)                    # 最终 URL 也校验（validate_url_target 要求公网 IP）
        r = client.get(resolved, follow_redirects=False)
        if r.is_redirect:
            raise RuntimeError(f"重定向未解析完整: {url} -> {resolved}")
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
        get_rag_service().index_document(str(dest))   # 写盘后做索引热更新
        return ToolResult(text=f"已下载 PDF: {dest}")
