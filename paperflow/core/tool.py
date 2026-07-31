# paperflow/core/tool.py
"""
Tool 抽象基类 —— 所有 Agent 可调用工具的契约定义。

设计依据 ADR 0003（权限最小化）：
- 每个 Tool 声明 ``risk_level``，后续 Layer 1 由 Policy Engine 根据风险等级决定
  allow / deny / confirm
- ``ToolResult.summary`` 字段从 Layer 0 即预留，供后续记忆系统写入结构化摘要
- Tool 实例统一通过 ``agents/<name>/tools.py`` 模块级 ``TOOLS`` 列表暴露，
  由 AgentRegistry 通过 importlib 动态加载
"""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass
class ToolResult:
    """
    工具执行结果，同时包含给 LLM 看的文本和给记忆系统用的结构化摘要。

    .. note::

        ``summary`` 默认空 dict，Layer 0 不写入，但字段已预留。
        后续 Layer 的记忆系统通过此字段提取可沉淀的结构化信息，
        避免后续改动所有 Tool 的 execute 签名。
    """

    #: 给 LLM 看的完整结果文本，直接拼接到 ReAct 循环的 tool message 中
    text: str

    #: 结构化摘要，供 Experience Memory / Dream 后台消费（Layer 0 仅占位）
    summary: dict = field(default_factory=dict)


class Tool(ABC):
    """
    工具抽象基类，定义所有 Agent 可调用工具的接口契约。

    子类必须定义类级别的 ``name``、``description``、``parameters`` 属性，
    并实现 ``execute(**kwargs)`` 方法。

    ``parameters`` 使用 JSON Schema 格式描述参数，
    直接喂给 LLM 的 function calling / tool use 机制，
    不做中间抽象层，保证与 OpenAI API 的兼容性。

    ``risk_level`` 分级（Layer 0 仅声明，Layer 1 由 Policy Engine 执行）:

    - ``"low"``     只读操作，无副作用（搜索、读取）
    - ``"medium"``  写操作，影响本地文件（写入笔记、下载 PDF）
    - ``"high"``    修改/删除操作（覆盖文件、重命名）
    - ``"critical"`` 不可逆操作（删除文件）
    """

    #: 工具名称，供 LLM function calling 的 tool_choice 使用
    name: str

    #: 工具描述，告诉 LLM 何时以及如何使用此工具
    description: str

    #: JSON Schema 格式的参数定义，直接传递给 OpenAI function calling API
    parameters: dict

    #: 风险等级，Layer 0 仅声明，Layer 1 由 Policy Engine 据此决定 allow/deny/confirm
    risk_level: str = "low"

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """
        执行工具逻辑，返回结果文本和可选的结构化摘要。

        :param kwargs: LLM 生成的参数，键名与 ``parameters`` schema 中的属性名一致
        :returns: ToolResult，包含给 LLM 的文本和给记忆系统的摘要
        :raises Exception: 执行失败时由 Agent._exec_tool 捕获并转为错误 ToolResult
        """
        ...
