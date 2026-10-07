"""记忆层的领域异常。"""


class ConcurrentUpdateError(Exception):
    """并发写冲突：目标资源的版本在本次读-改-写期间已被其他事务推进。

    出现即说明有人绕过了 BlockManager 的原子入口（跨进程写、或直接调 orm）。
    进程内所有写路径都经 mutate_block 的单次持锁，正常情况下不会触发。
    """

    def __init__(self, resource_id: str) -> None:
        super().__init__(f"resource {resource_id} was updated concurrently")
        self.resource_id = resource_id
