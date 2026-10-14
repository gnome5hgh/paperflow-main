"""模型响应的信封：响应对象里除正文与工具调用之外的一切。

它挂在 `AssistantMessage.meta` 上，但本身不是一种消息——`messages/` 只放消息类型，
信封与 `Usage` 作为层间载体（客户端产出、Agent、审计与持久化消费）留在这里。

信封**随 assistant 消息落盘**（messages 表一列 JSON），所以本模块同时持它的
dict 往返：落盘方只调这里的函数，不自己拼键名——将来端点多报一个字段，只改这一处。
"""
from dataclasses import asdict, dataclass, fields

from paperflow.core.llm.domain.dto.usage import Usage

__all__ = ["ResponseMeta", "response_meta_to_dict", "response_meta_from_dict"]


@dataclass(frozen=True)
class ResponseMeta:
    """一次模型响应的信封。

    正文与 tool_calls 进 wire、进上下文窗口；信封用于四件事：判断是否被截断
    （``finish_reason``）、核对 token 用量（``usage``）、排查响应异常（``refusal``
    与端点自报的模型快照）、事后核算与取证（落盘后按会话/日期汇总，或查一次回答
    到底由哪个模型快照产出）。

    **不上 wire**：出站序列化只取正文与 tool_calls。它以 JSON 一列随 assistant
    消息落盘到 messages 表（见 ``response_meta_to_dict``），回读的唯一出口是持久化
    消息 ``StoredMessage.meta``；审计的 telemetry 元数据也从同一个信封派生，是另一条
    并行通道（JSONL、按天轮转、不绑消息身份）。

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


#: 本类型与内嵌 Usage 的已知字段名——重建时按它们过滤，见 response_meta_from_dict。
_META_FIELDS = frozenset(f.name for f in fields(ResponseMeta))
_USAGE_FIELDS = frozenset(f.name for f in fields(Usage))


def response_meta_to_dict(meta: ResponseMeta) -> dict:
    """信封 → 可 JSON 序列化的 dict（内嵌的 Usage 一并展开成子 dict）。

    Args:
        meta: 一次模型响应的信封。

    Returns:
        纯 dict/list/标量组成的结果，可直接 ``json.dumps``。
    """
    return asdict(meta)


def response_meta_from_dict(d: dict) -> ResponseMeta:
    """dict → 信封（落盘回读入口）。

    端点与版本都会漂移，故按已知字段名**过滤**而不是直接展开：新版本写下的、
    本版本不认识的键（或 Usage 里多出来的键）静默丢弃，本版本新增的字段在旧行上
    取默认 None——两个方向的漂移都不会让回读抛异常。

    Args:
        d: 落盘时由 response_meta_to_dict 生成的 dict。

    Returns:
        ResponseMeta；输入缺字段时该项为默认 None。
    """
    raw_usage = d.get("usage")
    usage = None
    if isinstance(raw_usage, dict):
        usage = Usage(**{k: v for k, v in raw_usage.items() if k in _USAGE_FIELDS})
    return ResponseMeta(
        usage=usage,
        **{k: v for k, v in d.items() if k in _META_FIELDS and k != "usage"},
    )
