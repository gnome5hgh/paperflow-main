"""一次压缩调用的整合输出：摘要 + 任务快照。

两者由同一次结构化调用产出，避免两次 LLM 请求，也避免摘要与快照对
同一件事给出互相矛盾的说法。生成后分别渲染成两条消息：
摘要进对话历史，快照放在窗口末尾。
"""
from pydantic import BaseModel, Field

from paperflow.core.memory.domain.entity.summary import Summary
from paperflow.core.memory.domain.entity.task_snapshot import TaskSnapshot

__all__ = ["CompactionResult"]


class CompactionResult(BaseModel):
    """压缩调用的完整输出。

    Attributes:
        summary: 模型生成的 8 节摘要内容。
        snapshot: 同一时刻抽出的任务边界清单。
    """

    summary: Summary
    snapshot: TaskSnapshot = Field(default_factory=TaskSnapshot)
