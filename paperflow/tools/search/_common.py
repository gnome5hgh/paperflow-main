# paperflow/tools/search/_common.py
"""搜索模块公共设施 + 双源搜索基类(私有共享模块,不进 __init__ 导出)。

本模块承载两部分:
1. **搜索公共状态**:每轮去重论文池(SearchRunState/get_run_state,由核心运行时
   懒注入给声明 wants_run_state 的搜索工具)、模块级查询缓存(LRU)与源熔断
   (query_cache/breaker)。
2. **BaseSearchTool**:arxiv/openalex 双源搜索的共享 execute() 骨架(模板方法),
   子类只实现源标识/客户端/结果渲染差异段。

SSRF 安全抓取走 paperflow/tools/common/_http.py 的共享 mixin；PDF 下载职责已拆至
fetch_pdf.py 的 FetchPdfTool——本模块只做纯搜索,不再触碰本地资料库。
"""
import re
import time
from collections import OrderedDict

from paperflow.core.security.network import SSRFError
from paperflow.core.tool import Tool, ToolResult

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


class BaseSearchTool(Tool):
    """双源搜索共享骨架(模板方法)。子类覆写 _source/_make_client/_render_lines。"""

    #: 搜索源名:熔断键与缓存键的前缀(子类覆写为 "arxiv"/"openalex")
    _source: str = ""
    #: 熔断提示里的显示名(子类覆写,如 "arXiv")
    _breaker_name: str = ""
    #: 熔断时建议换用的备选工具名(子类覆写,如 "openalex_search")
    _alternate: str = ""

    #: 纯搜索是只读操作(仅出站抓取元数据)——只读会话(low)即可放行;写盘已拆至 fetch_pdf
    risk_level = "low"
    allowed_roots = []
    #: 返回外部内容(标题/摘要/URL),需打"未经安全校验"横幅
    output_scan = "mark"
    side_effects = ["network"]
    #: 声明后执行器会按追踪 ID 注入每轮共享的去重池;未声明的工具零开销
    wants_run_state = True

    def __init__(self):
        super().__init__()
        # 懒建客户端:缓存命中或熔断短路时无需走网络(测试可经 _make_client 注入 MockTransport)
        self._client = None

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        """构造源客户端;测试经 transport/ssrf_check 注入 MockTransport 与 SSRF 桩。"""
        raise NotImplementedError

    def _render_lines(self, papers: list[dict]) -> list[str]:
        """把搜索结果渲染成 LLM 可见行(含来源标记;空结果返回空列表)。"""
        raise NotImplementedError

    def execute(self, query: str, max_results: int = 5,
                year_from: int | None = None, year_to: int | None = None,
                _run_state=None) -> ToolResult:
        # 兜底钳制:模型可能绕过参数 schema 传极端数量(如 1 或 100),越界会让下游
        # reviewer 门禁超预算,故 clamp 到 [3,50] 保证结果规模可控。
        max_results = min(max(3, max_results), 50)
        # 查询缓存:键为(源, query, 年份区间, 钳制后数量),不同源/参数视为不同检索。
        # 命中缓存直接复用,不重复打 API 也不重复入池。下载已拆为独立 fetch_pdf 工具,
        # 不经过本缓存,无需再为下载意图做缓存旁路。
        ckey = (self._source, query, year_from, year_to, max_results)
        cached = query_cache_get(ckey)
        if cached is not None:
            return ToolResult(text=f"（缓存）该 query 已搜索过，结果同上；如需不同结果请调整检索词。\n{cached}")
        # 源熔断:连续失败达到阈值时本来源短期短路,提示改用备选源(由 LLM 决策)。
        if breaker_is_open(self._source):
            return ToolResult(text=f"{self._breaker_name} 连续失败已熔断，本次任务请改用 {self._alternate}")
        client = self._client or self._make_client()
        try:
            papers = client.search(query, max_results, year_from, year_to)
            breaker_register_success(self._source)
        except SSRFError as e:
            # SSRF 违规是安全拦截而非源故障,不计数熔断;返回错误让 LLM 调整目标
            return ToolResult(text=f"SSRF blocked: {e}")
        except Exception as e:
            # 网络超时/连接错误等真实源故障 → 记一次失败(熔断计数)
            breaker_register_failure(self._source)
            return ToolResult(text=f"Tool error: {e}")
        if _run_state is not None:
            # 结果入每轮共享去重池(add 内部按 DOI→arXiv ID→规范化标题四级键去重合并,
            # 跨源同论文只留一条)。_run_state 由执行器作为独立 kwarg 传入,不进审计参数。
            _run_state.add(papers)
        lines = self._render_lines(papers)
        # 仅非空结果写缓存:空结果被缓存会让重复 query 返回「（缓存）…」而非「无搜索
        # 结果」,语义不一致。
        if papers:
            query_cache_put(ckey, "\n".join(lines))
        return ToolResult(text="\n".join(lines) if lines else "无搜索结果")
