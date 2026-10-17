"""轮末记忆整合的后台 debounce 队列。

轮末只 enqueue，不阻塞回答收尾；同一会话在静默窗口内的多次 enqueue 合并成
一次整合。连续活跃的会话等不到静默窗口时，由 max_delay 强制触发。退出、
SIGINT、异常路径由调用方 flush，保证本轮沉淀不丢。

队列只承载会话信号，不承载消息：整合器仍按游标从 SQL 读新增消息，所以反复
enqueue 不会丢批次，也不会破坏「抽取失败保留游标」的既有语义。

实现用主事件循环上的 async 任务，而不是线程 + Timer：整合器与 LLM 客户端都是
异步且绑定事件循环；REPL 的输入已通过 asyncio.to_thread 让出循环，空闲时定时
任务能正常触发。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = ["ConsolidationQueue"]


@dataclass
class _SessionState:
    """单个会话的排队状态。

    Attributes:
        first_enqueued_at: float | None，本轮批次的首次入队时刻（max_delay 起点）
        last_enqueued_at: float，最近一次入队时刻（debounce 起点）
        started_at: float，本次整合开始时刻（判断运行期间是否有新入队）
        task: asyncio.Task | None，当前定时任务
        lock: asyncio.Lock，同一会话同时只跑一次整合
    """

    first_enqueued_at: float | None = None
    last_enqueued_at: float = 0.0
    started_at: float = 0.0
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConsolidationQueue:
    """按会话合并的记忆整合队列（单会话实例；多会话时每会话一个）。

    Attributes:
        _consolidator: MemoryConsolidator，真正执行整合的对象
        _session_id: str，会话标识（日志用）
        _debounce: float，静默窗口（秒）
        _max_delay: float，强制整合上限（秒）
        _state: _SessionState，排队状态
        _closed: bool，flush 后关闭，不再接受新入队
    """

    def __init__(self, consolidator, session_id: str, *,
                 debounce_seconds: int = 30, max_delay_seconds: int = 300):
        """初始化队列。

        Args:
            consolidator: MemoryConsolidator，整合执行者。
            session_id: str，会话标识（日志用）。
            debounce_seconds: int，静默窗口（秒）。
            max_delay_seconds: int，强制整合上限（秒），不小于静默窗口。
        """
        self._consolidator = consolidator
        self._session_id = session_id
        self._debounce = max(0.0, float(debounce_seconds))
        self._max_delay = max(self._debounce, float(max_delay_seconds))
        self._state = _SessionState()
        self._closed = False

    def enqueue(self) -> None:
        """登记一次「本轮有新对话」。

        同一批次的多次调用只更新时间戳；已有定时任务时不再新建。
        """
        if self._closed or self._consolidator is None:
            return
        now = time.monotonic()
        state = self._state
        if state.first_enqueued_at is None:
            state.first_enqueued_at = now
        state.last_enqueued_at = now
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(self._debounce_loop())

    async def _debounce_loop(self) -> None:
        """等静默窗口（或 max_delay）后执行一次整合；运行期间有新入队则补跑。"""
        state = self._state
        if state.first_enqueued_at is None:
            state.first_enqueued_at = state.last_enqueued_at
        while True:
            deadline = min(state.last_enqueued_at + self._debounce,
                           state.first_enqueued_at + self._max_delay)
            wait = deadline - time.monotonic()
            if wait <= 0:
                break
            await asyncio.sleep(wait)
        state.started_at = time.monotonic()
        async with state.lock:
            try:
                await self._consolidator.consolidate()
            except Exception:
                logger.warning("后台记忆整合失败（会话 %s）",
                               self._session_id, exc_info=True)
        # 运行期间又有新消息：从新的批次重新排队一次。
        if state.last_enqueued_at > state.started_at:
            state.first_enqueued_at = state.last_enqueued_at
            state.task = asyncio.create_task(self._debounce_loop())
        else:
            state.first_enqueued_at = None
            state.task = None

    async def flush(self) -> None:
        """立即跑完待处理整合（退出/SIGINT/异常路径调用）。"""
        if self._consolidator is None:
            return
        self._closed = True
        state = self._state
        if state.task is not None and not state.task.done():
            state.task.cancel()
            try:
                await state.task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("等待整合任务结束时出错", exc_info=True)
        if state.first_enqueued_at is None:
            return
        async with state.lock:
            try:
                await self._consolidator.consolidate()
            except Exception:
                logger.warning("退出前记忆整合失败（会话 %s）",
                               self._session_id, exc_info=True)
        state.first_enqueued_at = None
        state.task = None

    @property
    def pending(self) -> bool:
        """是否有待处理的整合。"""
        return self._state.first_enqueued_at is not None
