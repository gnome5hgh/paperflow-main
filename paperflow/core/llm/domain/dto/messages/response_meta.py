"""模型响应的信封：响应对象里除正文与工具调用之外的一切。"""
from dataclasses import dataclass

from paperflow.core.llm.domain.dto.messages.usage import Usage

__all__ = ["ResponseMeta"]


@dataclass(frozen=True)
class ResponseMeta:
    """一次模型响应的信封。

    正文与 tool_calls 进 wire、进上下文窗口；信封留在本地，用于三件事：判断是否被
    截断（``finish_reason``）、核对 token 用量（``usage``）、排查响应异常（``refusal``
    与端点自报的模型快照）。

    **不落盘**：它随消息对象活在进程内的 in-context 窗口里，重启即丢；需要长期留存
    的元数据走审计那条通道。

    Attributes:
        id: 端点给的补全 id
        model: 端点自报的模型快照（常带版本后缀，比配置串精确）
        created: 端点给的创建时间戳（Unix 秒）
        finish_reason: 结束原因（stop / length / tool_calls / content_filter …）
        usage: token 用量，端点未报时为 None
        refusal: 模型拒绝作答时的说明文本
        reasoning_content: 思维链正文（部分端点的非标准字段）
        system_fingerprint: 端点后端配置指纹
    """

    id: str | None = None
    model: str | None = None
    created: int | None = None
    finish_reason: str | None = None
    usage: Usage | None = None
    refusal: str | None = None
    reasoning_content: str | None = None
    system_fingerprint: str | None = None
