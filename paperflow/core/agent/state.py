"""运行期状态容器——按「活多久」分成两个显式作用域。

- SessionState（键 session_id）：跨 run 存活。装「跨任务才有意义」的东西——连续失败
  计数（连续失败才升级）。
- RunState（键 trace_id）：一次用户任务一个。装本次任务的中间产物——搜索去重池、派发与
  产物账本、各类预算计数、在途写占用、在途派发去重注册表。子 agent 继承父的
  trace_id，故整棵任务树共用一份。

两者都按 TTL 惰性清扫（取用时顺手剔除过期条目，不需要定时任务），粒度不同：
session 逐条过期（过窗的失败计数），run 整份丢弃（trace 每次 run 重新生成、永不复用，
所以整份删掉是安全的，预算也因此天然重置；去重注册表也随之只在一次 run 内存在）。
"""
from __future__ import annotations

import threading
import time

__all__ = [
    "RunState", "SessionState", "get_run_state", "get_session_state",
    "FAILURE_COUNT_TTL_S", "RUN_STATE_TTL_S",
]

#: 失败升级计数的存活窗（秒）：语义是「同一段工作里连续失败」——窗口太短会让
#: 升级提示失去意义，但也不能永不回收。
FAILURE_COUNT_TTL_S = 3600
#: run 作用域整份状态的存活窗（秒）：只做内存卫生，trace 每次 run 随机生成、永不复用。
RUN_STATE_TTL_S = 3600


class SessionState:
    """同一会话内跨 run 存活的运行期状态。

    只剩连续失败计数——spawn 去重注册表已迁到 RunState（它只需活在一次 run 内）。
    「连续失败」的语义天然跨轮（上一轮失败、这一轮又失败才升级），故留在会话作用域。

    Attributes:
        failure_counts: dict[str, int]，agent_type → 连续失败次数（成功即清零）
        failure_counts_at: dict[str, float]，失败计数最近写入时刻（TTL 清扫依据）
    """

    def __init__(self) -> None:
        """初始化空会话状态（失败计数为空）。"""
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
        writing_paths: dict[str, tuple[str, int]]，目标路径 → (持有者实例 id, 重入计数)（在途写互斥）
        spawn_registry: dict[tuple[str, str], float]，(父实例 id, 任务指纹) → 注册时刻（在途派发去重）
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
        #: 在途写占用：目标绝对路径 -> (持有者实例 id, 该持有者的重入计数)。只登记
        #: 「正在写的那一刻」——调用方在 finally 里释放，异常/取消都会走到，所以残留
        #: 条目只是防御性兜底，随整份容器被 TTL 回收。跨 agent 共享靠 trace：子 agent
        #: 继承父 trace，整棵任务树一份。键是工具解析出的真实写目标。
        self.writing_paths: dict[str, tuple[str, int]] = {}
        #: 写占用的登记锁：claim 的「看持有者 -> 计数自增」是复合操作，必须整体原子
        #: （与 spawn 侧守「检查-注册」同理）。当前调用方都在事件循环线程上，加的是
        #: 一层便宜保险。**它只护这张登记表，不串行化任何写入**——写入的排队在
        #: runtime 的路径锁、写入的原子性在 atomic_write，别把三者混作一谈。
        self._claim_lock = threading.Lock()
        #: 在途派发去重：(父实例 id, 任务指纹) -> 注册时刻。只登记「正在执行中」的派发，
        #: 完成即清除、不缓存结果——所以条目只活在一次 run 内，残留条目随整份容器被 TTL
        #: 回收，无需专门清扫。按父实例分桶：机械重复来自一次 LLM 生成（一个实例），分桶
        #: 足够，且不会让两个兄弟实例的同文本任务互相误拒。
        self.spawn_registry: dict[tuple[str, str], float] = {}
        #: 产物账本：落盘路径 -> 生产者（工具名）
        self.artifacts: dict[str, str] = {}
        #: 最后一次取用时刻（TTL 依据）：滑动窗口而非创建时刻，原因见 get_run_state。
        self.last_touched_at: float = time.monotonic()

    def claim_write(self, path: str, owner: str) -> str | None:
        """登记某实例正在写某路径；被另一个实例占用时返回其 id，登记成功返回 None。

        同一个 owner 重复登记只加计数——同一个 agent 在同一条消息里并行发两次同路径写，
        本就该交给路径锁串行，不该被自己拒掉。

        键就取「工具报上来的路径字符串」本身，不做任何归一化（不 resolve、不折叠 `.`/`..`、
        不去尾斜杠）——合约如此：`/a/./b.md` 与 `/a/b.md` 是两个不同的键。这是有意为之，
        不是疏漏：确认键、路径锁、写占用键都取自同一个 `effective_target_path` 字符串，
        若在这里替写占用单独归一化，它就会和另两把键错位，正是本设计要消除的键漂移。

        Args:
            path: str，目标绝对路径（工具解析出的真实写目标）
            owner: str，持有者实例 id（Agent._instance_id）

        Returns:
            None 表示登记成功；否则返回当前持有者的实例 id，调用方据此拒绝本次写。
        """
        with self._claim_lock:
            held = self.writing_paths.get(path)
            if held is not None and held[0] != owner:
                return held[0]
            self.writing_paths[path] = (owner, held[1] + 1 if held else 1)
            return None

    def release_write(self, path: str, owner: str) -> None:
        """释放一次写占用；计数归零即删条目。

        owner 不匹配时直接返回——防止误释放别人的占用（例如登记被拒的那次调用根本没登记）。

        Args:
            path: str，目标绝对路径
            owner: str，持有者实例 id
        """
        with self._claim_lock:
            held = self.writing_paths.get(path)
            if held is None or held[0] != owner:
                return
            if held[1] <= 1:
                self.writing_paths.pop(path, None)
            else:
                self.writing_paths[path] = (owner, held[1] - 1)


_RUN_STATES: dict[str, RunState] = {}
_SESSION_STATES: dict[str, SessionState] = {}
#: 容器自身的读写锁：并行 spawn 会同时取用，建容器与清扫必须整体原子。
_LOCK = threading.RLock()


def _sweep_session(st: SessionState, now: float) -> None:
    """剔除过窗的失败计数（会话容器已无其它需清扫的条目）。

    Args:
        st: SessionState，待清扫的会话容器
        now: float，当前单调时钟时刻

    Returns:
        无返回值（就地剔除过窗的失败计数）。
    """
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
