"""压缩摘要中由模型生成的 8 节内容。

摘要固定 9 节，模型只写其中 8 节：
1. 核心请求与意图（primary_request_and_intent）
2. 关键技术概念（key_technical_concepts）
3. 文件与代码片段（files_and_code_sections）
4. 错误与修复（errors_and_fixes）
5. 解决过程（problem_solving）
7. 待办任务（pending_tasks）
8. 压缩前正在做的事（current_work）
9. 下一步（optional_next_step）

第 6 节「All user messages」不在这里：那一节由代码从数据库逐字拼入，
不让模型转述，避免概括时漏掉硬约束和用户的原始措辞。
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
