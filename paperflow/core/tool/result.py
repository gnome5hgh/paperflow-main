"""ToolResult：一次工具执行的结果对象。

三个消费者各取所需（三通道互不污染）——``text`` 给 LLM 读、``summary`` 给记忆系统写、
``completion`` 给 CLI 渲染完成行。
"""
from dataclasses import dataclass, field


@dataclass
class ToolResult:
    """一次工具执行的结果对象，被三个消费者各取所需（三通道互不污染）：
    - ``text``：完整语义文本，给 LLM 读（进入 ReAct 对话流）
    - ``summary``：结构化副作用摘要，给记忆系统写（默认空 dict，为记忆层预留的前瞻钩子）
    - ``completion``：终端完成摘要，给 CLI 渲染完成行；与 LLM 面 text 解耦，
      LLM 读不到这一行，避免语义污染
    ``summary`` 用 ``field(default_factory=dict)`` 保证每个实例拿到独立 dict，
    不共享同一个可变默认值。

    Attributes:
        text: str，完整语义文本（给 LLM 读，进入 ReAct 对话流）
        summary: dict，结构化副作用摘要（给记忆系统写；默认空 dict）
        completion: str | None，终端完成摘要（给 CLI 渲染完成行；LLM 读不到）
        is_error: bool，错误结果标记（安全扫描不套「外部内容」横幅）
    """
    text: str
    summary: dict = field(default_factory=dict)
    #: 终端完成摘要（如 "File written: <path>"），_exec_tool 见非空则发完成状态行
    completion: str | None = None
    #: 错误结果标记：安全扫描对错误结果不套「外部内容」横幅
    is_error: bool = False
