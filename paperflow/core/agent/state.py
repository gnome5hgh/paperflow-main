"""运行期状态容器——session 与 run 两个显式作用域。

一次用户任务里的运行期状态过去散在四个模块级字典里，键形与生命周期各不相同（其中
两个从不清理）。本模块把它们收成两个容器：session 作用域跨 run 存活（同一会话内的
spawn 去重与失败计数），run 作用域按用户任务隔离（搜索去重池、派发与产物账本、各类
预算计数）。两者都按 TTL 惰性清扫——取用时顺手剔除过期条目，不需要定时任务。
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
#: 失败升级计数的存活窗（秒）：语义是「同一段工作里连续失败」，窗口太短会让升级提示
#: 失去意义，但也不能永不回收。
FAILURE_COUNT_TTL_S = 3600
#: run 作用域整份状态的存活窗（秒）：只做内存卫生，trace 每次 run 随机生成、永不复用。
RUN_STATE_TTL_S = 3600


class SessionState:
    """同一会话内跨 run 存活的运行期状态。"""

    def __init__(self) -> None:
        #: spawn 去重：任务指纹 -> {"state": "running"|"done", "result", "started_at"}
        self.spawn_registry: dict[str, dict] = {}
        #: 连续失败计数：agent_type -> 次数（成功即清零）
        self.failure_counts: dict[str, int] = {}
        #: 失败计数的最近写入时刻：agent_type -> monotonic（TTL 清扫依据）
        self.failure_counts_at: dict[str, float] = {}


class RunState:
    """一次用户任务（一个 trace）内的运行期状态。"""

    def __init__(self) -> None:
        #: 搜索负缓存：失败 URL -> 失败原因（拒绝重复尝试）
        self.failed_urls: dict[str, str] = {}
        #: 搜索成功短路：URL 或规范化标题 -> 落盘路径
        self.downloaded: dict[str, str] = {}
        #: supervisor 自身派发账本：(agent_type, status)
        self.spawn_dispatches: list[tuple[str, str]] = []
        #: 每轮派发计数：turn -> 次数（仅统计 supervisor 自身的派发）
        self.turn_spawn_counts: dict[int, int] = {}
        #: 审稿预算计数：(父实例 id, mode) -> 次数
        self.review_counts: dict[tuple[str, str], int] = {}
        #: 在途写盘目标路径：被正在执行的 spawn 占用时，同路径的新 spawn 被拒
        self.in_flight_paths: set[str] = set()
        #: 产物账本：落盘路径 -> 生产者（工具名）
        self.artifacts: dict[str, str] = {}
        #: 最后一次取用时刻（TTL 清扫依据）：是滑动窗口而非创建时刻——活跃任务每次取
        #: 容器都会把它推进，因此任务再长也不会被自己的清扫删掉，只有真正闲置的才回收。
        self.last_touched_at: float = time.monotonic()


_RUN_STATES: dict[str, RunState] = {}
_SESSION_STATES: dict[str, SessionState] = {}
#: 容器自身的读写锁：并行 spawn 会同时取用，建容器与清扫必须整体原子。
_LOCK = threading.RLock()


def _sweep_session(st: SessionState, now: float) -> None:
    """剔除过窗的 done 缓存与失败计数（running 条目不动——可能正被另一线程执行）。"""
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
    """取该会话的状态容器（不存在则建），取用时顺手清扫过期条目。"""
    with _LOCK:
        now = time.monotonic()
        st = _SESSION_STATES.get(session_id)
        if st is None:
            st = _SESSION_STATES[session_id] = SessionState()
        _sweep_session(st, now)
        return st


def get_run_state(trace_id: str) -> RunState:
    """取该次用户任务的状态容器（不存在则建），取用时顺手丢弃闲置过久的整份 run 状态。

    回收按滑动窗口：命中已存在的容器先刷新它的取用时刻，再做清扫——若先扫后取，一个
    存活超过窗口的活跃任务会在自己的取用调用里被删掉又立刻重建，中途积累的搜索负缓存
    与成功短路会被静默清空。
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
