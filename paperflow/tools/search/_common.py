# paperflow/tools/search/_common.py
"""搜索模块公共设施(私有共享模块,不进 __init__ 导出)。

本模块承载搜索公共状态:每轮去重论文池(SearchRunState/get_run_state,由核心运行时
懒注入给声明 wants_run_state 的搜索工具)、模块级查询缓存(LRU)与源熔断
(query_cache/breaker)。WebSearchTool 工具本体在 web_search.py,按 source 分发客户端。

SSRF 安全抓取走 paperflow/tools/common/_http.py 的共享 mixin；PDF 下载职责已拆至
fetch_pdf.py 的 FetchPdfTool——本模块只做纯搜索公共状态,不再触碰本地资料库。
"""
import re
import time
from collections import OrderedDict

#: 查询缓存上限(LRU 逐出)
QUERY_CACHE_MAX = 20
#: 熔断阈值与冷却:连续失败达到阈值即熔断,冷却期后复位
BREAKER_THRESHOLD = 2
BREAKER_COOLDOWN_S = 300


def _norm_title(title: str) -> str:
    """规范化标题:去非字母数字,转小写——跨源同论文的兜底去重键。"""
    return re.sub(r"[^\w]", "", title).lower()


class SearchRunState:
    """每轮自动去重论文池。pool: 去重键 → paper dict(同一键跨源合并)。"""

    def __init__(self) -> None:
        self.pool: dict[str, dict] = {}

    @staticmethod
    def dedup_key(p: dict) -> str:
        """四级去重键:DOI → arXiv ID → 规范化标题(跨源同论文合并的依据)。"""
        if p.get("doi"):
            return f"doi:{p['doi']}"
        if p.get("arxiv_id"):
            return f"arxiv:{p['arxiv_id']}"
        return f"title:{_norm_title(p.get('title', ''))}"

    def add(self, papers: list[dict]) -> list[dict]:
        """插入并去重;同键跨源合并缺失来源字段,返回新增论文列表。"""
        added: list[dict] = []
        for p in papers:
            k = self.dedup_key(p)
            if k in self.pool:
                existing = self.pool[k]
                for field in ("pdf_url", "openalex_id", "arxiv_id", "venue", "issn"):
                    if not existing.get(field) and p.get(field):
                        existing[field] = p[field]
                continue
            self.pool[k] = p
            added.append(p)
        return added

    def as_candidates(self) -> list[dict]:
        return list(self.pool.values())


#: 每轮去重池注册表:追踪 ID → SearchRunState
_RUN_STATES: dict[str, SearchRunState] = {}


def get_run_state(trace_id: str) -> SearchRunState:
    """取/建当前 run 的去重池(核心运行时每次工具调用注入同一个实例)。"""
    st = _RUN_STATES.get(trace_id)
    if st is None:
        st = _RUN_STATES[trace_id] = SearchRunState()
    return st


# ── 模块级查询缓存(LRU)──
_QUERY_CACHE: OrderedDict[tuple, str] = OrderedDict()


def query_cache_get(key: tuple) -> str | None:
    """取缓存;命中视为最近使用(移到 LRU 末尾),未命中返回 None。"""
    if key in _QUERY_CACHE:
        _QUERY_CACHE.move_to_end(key)
        return _QUERY_CACHE[key]
    return None


def query_cache_put(key: tuple, text: str) -> None:
    """写缓存并维持 LRU:新写入视为最近使用,超上限逐出最久未用的条目。"""
    _QUERY_CACHE[key] = text
    _QUERY_CACHE.move_to_end(key)
    while len(_QUERY_CACHE) > QUERY_CACHE_MAX:
        _QUERY_CACHE.popitem(last=False)


# ── 模块级源熔断(时间有界)──
_SOURCE_BREAKER: dict[str, dict] = {}


def breaker_is_open(source: str) -> bool:
    """源是否处于熔断状态:失败计数达阈值且仍在冷却期内 → True。"""
    b = _SOURCE_BREAKER.get(source)
    if not b:
        return False
    if b["failures"] >= BREAKER_THRESHOLD and \
            time.monotonic() - b["opened_at"] < BREAKER_COOLDOWN_S:
        return True
    # 冷却已过则复位:但只有失败计数已达阈值才清零——否则每次检查都会把未达阈值的
    # 失败计数一并清掉,连续失败永远攒不到阈值,熔断形同虚设。
    if b["failures"] >= BREAKER_THRESHOLD:
        _SOURCE_BREAKER[source] = {"failures": 0, "opened_at": 0.0}
    return False


def breaker_register_failure(source: str) -> None:
    """记一次源失败;失败计数达到阈值时记录熔断起点时间。"""
    b = _SOURCE_BREAKER.get(source) or {"failures": 0, "opened_at": 0.0}
    b["failures"] += 1
    if b["failures"] >= BREAKER_THRESHOLD:
        b["opened_at"] = time.monotonic()
    _SOURCE_BREAKER[source] = b


def breaker_register_success(source: str) -> None:
    """记一次源成功:复位该源的失败计数与熔断状态。"""
    _SOURCE_BREAKER[source] = {"failures": 0, "opened_at": 0.0}
