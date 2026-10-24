"""压缩摘要中由模型生成的 8 节内容。

1. 核心请求与意图（primary_request_and_intent）
2. 关键技术概念（key_technical_concepts）
3. 文件与代码片段（files_and_code_sections）
4. 错误与修复（errors_and_fixes）
5. 解决过程（problem_solving）
6. 待办任务（pending_tasks）
7. 压缩前正在做的事（current_work）
8. 下一步（optional_next_step）

用户原话不进摘要：硬约束、截止时间等边界由任务快照逐字保留，
其余原话靠 SQL + conversation_search 回溯。
"""
from pydantic import BaseModel

__all__ = ["Summary"]


class Summary(BaseModel):
    """模型在一次压缩调用里写出的 8 节摘要字段。

    Attributes:
        primary_request_and_intent: 用户的核心请求与成功标准。
        key_technical_concepts: 关键技术概念与领域术语。
        files_and_code_sections: 涉及的论文、笔记、数据路径与脚本片段。
        errors_and_fixes: 遇到的错误与修复方式。
        problem_solving: 解决问题的过程与决策依据。
        pending_tasks: 仍未完成的事项。
        current_work: 压缩前一刻正在做的事。
        optional_next_step: 与最近一次用户请求直接相关的下一步。
    """

    primary_request_and_intent: str
    key_technical_concepts: str
    files_and_code_sections: str
    errors_and_fixes: str
    problem_solving: str
    pending_tasks: str
    current_work: str
    optional_next_step: str
