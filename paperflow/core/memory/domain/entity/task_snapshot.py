"""压缩时抽出的任务快照：目标、硬约束、截止时间、未完成步骤。

它是压缩时刻的边界清单，不是摘要的第 10 节。与摘要由同一次结构化调用
产出，但渲染成独立的消息放进窗口末尾，紧挨当前用户任务，避免边界信息
被长摘要和尾部对话冲淡。
"""
from pydantic import BaseModel, Field

__all__ = ["TaskSnapshot"]


class TaskSnapshot(BaseModel):
    """任务边界清单。

    Attributes:
        goal: 用一句话概括的用户目标。
        hard_constraints: 必须遵守或禁止的约束，逐字保留用户原话。
        deadline: 用户明确说出的截止时间；没有说过时为 None，禁止推断。
        unfinished_steps: 尚未完成的步骤。
    """

    goal: str = ""
    hard_constraints: list[str] = Field(default_factory=list)
    deadline: str | None = None
    unfinished_steps: list[str] = Field(default_factory=list)
