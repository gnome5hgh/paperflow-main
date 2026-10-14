"""检索熔断器：Milvus 连续失败即跳闸，冷却窗口后自动放一次探测。

为什么需要：Milvus 不可达时每次检索都要先等一轮连接超时。调用方（子 agent）的预算是按秒
计的，个别环境里一次检索能白等几十秒，一两次就把预算吃穿。跳闸期间直接给出「检索不可用」
的结论、省掉这段白等；冷却结束再放一次真实检索当探测，成功即复位、失败则重新跳闸，因此
Milvus 恢复后不需要重启进程。

只保护**检索**侧：索引写入失败有各自的可见产物与重试路径（索引状态文件、增量重扫），
不该被检索侧的瞬时故障牵连。
"""
import threading
import time

#: 连续失败多少次判定 Milvus 不可用。取 1：单次 RPC 已带秒级截止时间，能撞上截止时间
#: 说明对面不是「慢」而是「不可达」，再试一次只是多等一轮。
FAILURE_THRESHOLD = 1

#: 跳闸后的冷却秒数。期间检索直接返回降级结论，到期放行一次探测。
COOLDOWN_SECONDS = 60.0

# 状态名：closed 正常；open 跳闸中；half_open 已放行一次探测、等它回话。
_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"


class RetrievalBreaker:
    """三态熔断器（closed / open / half-open），自带锁。

    放行判断发生在进 RAGService 锁**之前**（进锁前判断才省得掉等待），所以状态由本类
    自己的锁保护，而不依赖调用方的锁。

    Attributes:
        state: str，当前状态名，供日志与测试观察。
    """

    def __init__(self, failure_threshold: int = FAILURE_THRESHOLD,
                 cooldown: float = COOLDOWN_SECONDS, clock=time.monotonic):
        """初始化熔断器。

        Args:
            failure_threshold: int，连续失败多少次跳闸。
            cooldown: float，跳闸后的冷却秒数。
            clock: 单调时钟函数，注入以便测试推进时间（用单调时钟而非墙钟，
                系统时间被回拨时冷却不会失准）。
        """
        self._threshold = failure_threshold
        self._cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._state = _CLOSED
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        """当前状态名（closed / open / half_open）。"""
        with self._lock:
            return self._state

    @property
    def retry_after(self) -> float:
        """冷却剩余秒数；不在冷却中时为 0。

        供降级提示告诉调用方「还要等多久才能再试」，避免它原地反复重试。
        """
        with self._lock:
            if self._state != _OPEN:
                return 0.0
            return max(0.0, self._cooldown - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        """本次检索是否放行。

        closed 态一律放行；open 态冷却未到不放行，冷却到期转 half-open 并放行一次
        探测（只放一次，探测没回来之前其余请求继续被挡）。

        Returns:
            bool: 放行为 True。
        """
        with self._lock:
            if self._state == _CLOSED:
                return True
            if self._state == _OPEN and self._clock() - self._opened_at >= self._cooldown:
                self._state = _HALF_OPEN
                return True
            return False

    def record_success(self) -> None:
        """记录一次成功检索：复位为 closed 并清零失败计数。"""
        with self._lock:
            self._state = _CLOSED
            self._failures = 0

    def record_failure(self) -> None:
        """记录一次失败检索：累计到阈值即跳闸，并重新开始计时冷却。

        探测（half-open）失败也算一次失败，于是重新跳闸、重新等一个冷却窗口——
        这就是「恢复前别拿请求去撞墙」的节流。
        """
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._state = _OPEN
                self._opened_at = self._clock()
