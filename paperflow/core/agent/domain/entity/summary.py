"""压缩摘要的结构化输出模型。

这是模型在一次压缩调用里要返回的字段；第 6 节「All user messages」不在
schema 里——它由代码从 SQL 逐字插入，避免模型转述用户原话或漏掉安全/授权类约束。
"""
from pydantic import BaseModel, Field

__all__ = ["SummarySchema"]


class SummarySchema(BaseModel):
    """模型输出的 8 个摘要字段。

    Attributes:
        primary_request_and_intent: str，用户的核心请求与成功标准
        key_technical_concepts: str，关键技术概念与领域术语
        files_and_code_sections: str，涉及的论文/笔记/数据路径/脚本片段
        errors_and_fixes: str，错误与修复过程
        problem_solving: str，问题解决过程与决策依据
        pending_tasks: str，待办任务
        current_work: str，压缩前一刻正在做的事
        optional_next_step: str，与最近用户请求直接相关的下一步
        goal: str，任务目标（与摘要同一次调用产出，运行层单独持久化）
        hard_constraints: list[str]，硬约束（逐字保留）
        deadline: str | None，截止时间；只在用户明确说出时填写
        unfinished_steps: list[str]，未完成步骤
    """

    primary_request_and_intent: str
    key_technical_concepts: str
    files_and_code_sections: str
    errors_and_fixes: str
    problem_solving: str
    pending_tasks: str
    current_work: str
    optional_next_step: str

    #: 任务锚点：与摘要同一次调用产出，随后由运行层单独持久化并每轮注入。
    goal: str = ""
    hard_constraints: list[str] = Field(default_factory=list)
    deadline: str | None = None
    unfinished_steps: list[str] = Field(default_factory=list)
