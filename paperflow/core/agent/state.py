"""运行期状态容器——按「活多久」分成两个显式作用域。

- SessionState（键 session_id）：跨 run 存活。装「跨任务才有意义」的东西——同一会话内
  的 spawn 去重（上一条消息派过的任务别重复派）与连续失败计数（连续失败才升级）。
- RunState（键 trace_id）：一次用户任务一个。装本次任务的中间产物——搜索去重池、派发与
  产物账本、各类预算计数、在途路径。子 agent 继承父的 trace_id，故整棵任务树共用一份。

两者都按 TTL 惰性清扫（取用时顺手剔除过期条目，不需要定时任务），但粒度不同：
session 逐条过期（过窗的 done 缓存与失败计数），run 整份丢弃（trace 每次 run 重新
生成、永不复用，所以整份删掉是安全的，预算也因此天然重置）。
"""
from __future__ import annotations

import threading
import time

__all__ = [
    "RunState", "SessionState", "get_run_state", "get_session_state",
    "SPAWN_REUSE_WINDOW_S", "FAILURE_COUNT_TTL_S", "RUN_STATE_TTL_S",
]

#: spawn done 缓存的可复用时间窗（秒）：窗口内同指纹（仅无路径任务）直接复用结果。
SPAWN_REUSE_WINDOW_S = 300
#: 失败升级计数的存活窗（秒）：语义是「同一段工作里连续失败」——窗口太短会让
#: 升级提示失去意义，但也不能永不回收。
FAILURE_COUNT_TTL_S = 3600
#: run 作用域整份状态的存活窗（秒）：只做内存卫生，trace 每次 run 随机生成、永不复用。
RUN_STATE_TTL_S = 3600


class SessionState:
    """同一会话内跨 run 存活的运行期状态。

    Attributes:
        spawn_registry: dict[str, dict]，任务指纹 → {state: running|done, result, started_at}（spawn 去重）
        failure_counts: dict[str, int]，agent_type → 连续失败次数（成功即清零）
        failure_counts_at: dict[str, float]，失败计数最近写入时刻（TTL 清扫依据）
    """

    def __init__(self) -> None:
        """初始化空会话状态（各注册表与计数为空）。"""
        #: spawn 去重：任务指纹 -> {"state": "running"|"done", "result", "started_at"}
        self.spawn_registry: dict[str, dict] = {}
        #: 连续失败计数：agent_type -> 次数（成功即清零）
        self.failure_counts: dict[str, int] = {}
        #: 失败计数的最近写入时刻：agent_type -> monotonic（TTL 清扫依据）
        self.failure_counts_at: dict[str, float] = {}


class RunState:
    """一次用户任务（一个 trace）内的运行期状态。

    Attributes:
        failed_urls: dict[str, str]，失败 URL → 失败原因（搜索负缓存）
        downloaded: dict[str, str]，URL 或规范化标题 → 落盘路径（搜索成功短路）
        spawn_dispatches: list[tuple[str, str]]，supervisor 自身派发账本 (agent_type, status)
        turn_spawn_counts: dict[int, int]，轮次 → 该轮派发次数（每轮上限用）
        review_counts: dict[tuple[str, str], int]，(父实例 id, mode) → 审稿次数（预算用）
        in_flight_paths: dict[str, set[str]]，父实例 id → 在途写盘目标路径集（同路径互斥用）
        artifacts: dict[str, str]，落盘路径 → 生产者工具名（产物账本）
        last_touched_at: float，最后一次取用时刻（TTL 滑动窗口清扫依据）
    """

    def __init__(self) -> None:
        """初始化空 run 状态（各账本与缓存为空，取用时刻记为当前）。"""
        #: 搜索负缓存：失败 URL -> 失败原因（拒绝重复尝试）
        self.failed_urls: dict[str, str] = {}
        #: 搜索成功短路：URL 或规范化标题 -> 落盘路径
        self.downloaded: dict[str, str] = {}
        #: supervisor 自身派发账本：(agent_type, status)。
        #: 按 trace 共享（子 agent 继承父 trace_id）；安全前提是只有 supervisor 会读写它——
        #: 子 agent 的 intent_enabled 恒为 False（spawn 不传意图管线）。将来若子 agent 拿到意图管线，须改按实例分桶。
        self.spawn_dispatches: list[tuple[str, str]] = []
        #: 每轮派发计数：turn -> 次数（仅统计 supervisor 自身的派发）
        self.turn_spawn_counts: dict[int, int] = {}
        #: 审稿预算计数：(父实例 id, mode) -> 次数
        self.review_counts: dict[tuple[str, str], int] = {}
        #: 在途写盘目标路径，按「占用它的父实例 id」分桶：只有同一父扇出的兄弟互斥，
        #: 祖先/后代不误伤（后代可能只读、或顺序依赖父产物）。判定与释放见 spawn 闸 ⑧。
        self.in_flight_paths: dict[str, set[str]] = {}
        #: 产物账本：落盘路径 -> 生产者（工具名）
        self.artifacts: dict[str, str] = {}
        #: 最后一次取用时刻（TTL 依据）：滑动窗口而非创建时刻，原因见 get_run_state。
        self.last_touched_at: float = time.monotonic()


_RUN_STATES: dict[str, RunState] = {}
_SESSION_STATES: dict[str, SessionState] = {}
#: 容器自身的读写锁：并行 spawn 会同时取用，建容器与清扫必须整体原子。
_LOCK = threading.RLock()


def _sweep_session(st: SessionState, now: float) -> None:
    """剔除过窗的 done 缓存与失败计数（running 条目不动——可能正被另一线程执行）。

    Args:
        st: SessionState，待清扫的会话容器
        now: float，当前单调时钟时刻

    Returns:
        无返回值（就地剔除过窗的 done 缓存与失败计数；running 条目不动）。
    """
    stale = [fp for fp, e in st.spawn_registry.items()
             if e.get("state") == "done"
             and now - e.get("started_at", now) > SPAWN_REUSE_WINDOW_S]
    for fp in stale:
        st.spawn_registry.pop(fp, None)
    for agent_type, ts in list(st.failure_counts_at.items()):
        if now - ts > FAILURE_COUNT_TTL_S:
            st.failure_counts_at.pop(agent_type, None)
            st.failure_counts.pop(agent_type, None)


def get_session_state(session_id: str) -> SessionState:
    """取该会话的状态容器（不存在则建），取用时顺手清扫过期条目。

    Args:
        session_id: str，会话标识

    Returns:
        该会话的状态容器（不存在则建；取用时顺手清扫过期条目）。
    """
    with _LOCK:
        now = time.monotonic()
        st = _SESSION_STATES.get(session_id)
        if st is None:
            st = _SESSION_STATES[session_id] = SessionState()
        _sweep_session(st, now)
        return st


def get_run_state(trace_id: str) -> RunState:
    """取该次用户任务的状态容器（不存在则建），取用时顺手丢弃闲置过久的整份 run 状态。

    回收按滑动窗口：命中已有容器先刷新取用时刻，再清扫——若先扫后取，一个存活超过窗口的
    活跃任务会在自己的取用调用里被删掉又重建，中途积累的搜索负缓存与成功短路会静默清空。

    Args:
        trace_id: str，本次用户任务的追踪标识

    Returns:
        该次任务的状态容器（不存在则建；取用时顺手回收闲置过久的整份 run 状态）。
    """
    with _LOCK:
        now = time.monotonic()
        rs = _RUN_STATES.get(trace_id)
        if rs is not None:
            rs.last_touched_at = now
        stale = [tid for tid, other in _RUN_STATES.items()
                 if now - other.last_touched_at > RUN_STATE_TTL_S]
        for tid in stale:
            _RUN_STATES.pop(tid, None)
        if rs is None:
            rs = _RUN_STATES[trace_id] = RunState()
        return rs
