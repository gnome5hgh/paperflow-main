"""记忆层的领域异常。"""


class ConcurrentUpdateError(Exception):
    """并发写冲突：目标资源的版本在本次读-改-写期间已被其他事务推进。

    出现即说明有人绕过了 BlockManager 的原子入口（跨进程写、或直接调 orm）。
    进程内所有写路径都经 mutate_block 的单次持锁，正常情况下不会触发。

    Attributes:
        resource_id: str，被并发推进的块标识（供调用方定位冲突目标）
    """

    def __init__(self, resource_id: str) -> None:
        """构造并发写冲突异常。

        Args:
            resource_id: str，发生并发冲突的块标识，写入异常消息并留存为属性
        """
        super().__init__(f"resource {resource_id} was updated concurrently")
        self.resource_id = resource_id


class BlockLimitExceeded(ValueError):
    """写入内容超过块的字符上限。

    单独成类（而不是裸 ValueError）是为了让调用方能识别「块写满了」并作出确定性
    处置——记忆整合据此把这一条换到下一个分册，而不是把整批编辑算作失败。继承
    ValueError 以保持既有的异常捕获口径。

    Attributes:
        label: str，被写满的块标识
        limit: int，该块的字符上限
    """

    def __init__(self, label: str, limit: int) -> None:
        """构造超限异常。

        Args:
            label: str，被写满的块标识
            limit: int，该块的字符上限
        """
        super().__init__(f"block {label} exceeds {limit} character limit")
        self.label = label
        self.limit = limit
