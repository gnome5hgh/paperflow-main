"""记忆领域实体：压缩产物与任务快照。"""
from paperflow.core.memory.domain.entity.compaction_result import CompactionResult
from paperflow.core.memory.domain.entity.summary import Summary
from paperflow.core.memory.domain.entity.task_snapshot import TaskSnapshot

__all__ = ["CompactionResult", "Summary", "TaskSnapshot"]
