# paperflow/tools/search/_common.py
"""搜索/下载公共运行状态（去重池），不再承载查询缓存/熔断。

本模块承载每轮去重论文池（SearchRunState/get_run_state，由核心运行时懒注入给
声明 wants_run_state 的搜索工具）：failed_urls 负缓存 + downloaded 成功短路。
直接检索客户端（web_search 及 arXiv/OpenAlex/S2 clients）已退役，检索收敛到
MCP（paper-search-mcp，见 paperflow/core/mcp/）。

SSRF 安全抓取走 paperflow/tools/common/_http.py 的共享 mixin；PDF 下载职责在
fetch_pdf.py 的 FetchPdfTool——本模块只做公共运行状态,不触碰网络与本地资料库。
"""
import re


def _norm_title(title: str) -> str:
    """规范化标题:去非字母数字,转小写——跨源同论文的兜底去重键。"""
    return re.sub(r"[^\w]", "", title).lower()


class SearchRunState:
    """每轮公共运行状态。

    failed_urls: 本任务内下载失败的 URL → 失败原因（负缓存）。真实会话复验
    （2026-09-06）发现：对 404 这类永久性失败，模型会在单次任务内反复重试同一
    URL（实测 19 次直到撞轮数上限）——fetch_pdf 据此拒绝重复尝试。

    downloaded: 本任务内已成功下载的 URL/规范化标题 → 落盘路径，fetch_pdf
    成功性短路的依据（同 URL 或同规范化标题重复调用直接返回既有路径）。
    """

    def __init__(self) -> None:
        self.failed_urls: dict[str, str] = {}
        self.downloaded: dict[str, str] = {}


#: 每轮去重池注册表:追踪 ID → SearchRunState
_RUN_STATES: dict[str, SearchRunState] = {}


def get_run_state(trace_id: str) -> SearchRunState:
    """取/建当前 run 的去重池(核心运行时每次工具调用注入同一个实例)。"""
    st = _RUN_STATES.get(trace_id)
    if st is None:
        st = _RUN_STATES[trace_id] = SearchRunState()
    return st
