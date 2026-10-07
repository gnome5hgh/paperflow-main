# paperflow/tools/search/_common.py
"""搜索/下载公共运行状态（去重池），不再承载查询缓存/熔断。

每轮去重池已收进运行期状态容器（paperflow/core/agent/state.py 的 RunState，由核心
运行时懒注入给声明 wants_run_state 的搜索工具）：failed_urls 负缓存 + downloaded
成功短路。本模块保留标题规范化 helper，并再导出 get_run_state 兼容既有导入点。
直接检索客户端（web_search 及 arXiv/OpenAlex/S2 clients）已退役，检索收敛到
MCP（paper-search-mcp，见 paperflow/core/mcp/）。

SSRF 安全抓取走 paperflow/tools/common/_http.py 的共享 mixin；PDF 下载职责在
fetch_pdf.py 的 FetchPdfTool——本模块只做公共运行状态,不触碰网络与本地资料库。
"""
import re

from paperflow.core.agent.state import get_run_state   # noqa: F401  （兼容既有导入点）


def _norm_title(title: str) -> str:
    """规范化标题:去非字母数字,转小写——跨源同论文的兜底去重键。"""
    return re.sub(r"[^\w]", "", title).lower()
