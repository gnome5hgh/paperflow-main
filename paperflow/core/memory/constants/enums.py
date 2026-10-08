"""记忆模块的枚举——消息角色与标题来源。"""

from enum import StrEnum

__all__ = ["MessageRole", "TitleSource"]


class MessageRole(StrEnum):
    """对话消息的角色枚举（与 OpenAI wire 的角色名一致）。

    用于标识每条消息的来源角色：
        - system: 系统提示词
        - user: 用户输入
        - assistant: 模型输出
        - tool: 工具调用结果（与 tool_call_id 关联）
    """

    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class TitleSource(StrEnum):
    """论文标题的命中来源。

    搜索元数据最权威且免费，其后是本地回退链；``NONE`` 表示全链未命中，
    此时 title 也为 None，由调用方提示用户提供标题。

    字段侧（TitleResult.source）不做封闭校验——搜索元数据层可能带上游来源
    标识（如库内 id），那不是本枚举的值域。
    """

    #: 搜索元数据命中
    SEARCH = "search"
    #: GROBID 解析 PDF 结构得到
    GROBID = "grobid"
    #: LLM 读首页推断
    LLM = "llm"
    #: pdftitle 布局启发式
    PDFTITLE = "pdftitle"
    #: PyMuPDF 首页启发式兜底
    PYMUPDF = "pymupdf"
    #: 全链未命中（title 同时为 None）
    NONE = ""
