"""Recall 持久化消息模型：Message / MessageRole。

与 paperflow/core/llm.py::Message（OpenAI wire 格式）区分：本类型是落盘到
messages 表的持久化消息，由 MessageManager 从 wire 格式转换生成，补上 id /
created_at 等存储字段。content 恒为字符串（str | None），落盘与回放都不做
JSON 类型猜测。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Message", "MessageRole"]


class MessageRole(Enum):
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


class Message(BaseModel):
    """持久化消息：含 Recall 检索所需的全部字段。

    tool_calls / tool_call_id 记录工具调用轨迹；step_id / run_id / otid 是
    审计与轨迹追踪的关联键。

    与 paperflow.core.llm.Message（wire 格式）的区别：
        - wire 格式的 content 可以是 str、list 或 dict（多模态或结构化内容），
          而本模型 content 保证为 str | None，便于 SQLite 存储与统一检索。
        - 本模型增加了持久化辅助字段：id、created_at，以及追踪字段 step_id 等。
        - 转换由 MessageManager._wire_to_schema 完成，确保类型安全。

    Attributes:
        id: str，唯一标识（"message-<hex>" 自动生成，messages 表主键）
        role: MessageRole，消息角色
        content: str | None，消息文本（多模态/结构化内容已拍平为字符串）
        tool_calls: list[dict]，assistant 携带的工具调用列表（落盘为 JSON）
        tool_call_id: str | None，tool 消息关联的调用 ID（非 tool 消息为空）
        step_id: str | None，工作流步骤标识
        run_id: str | None，单次运行标识
        otid: str | None，开放追踪 ID
        created_at: datetime | None，创建时间（UTC；缺省由 ORM 取当前时间）
    """

    # 自动生成唯一 ID（格式："message-<32位hex>"）
    # 由 ORM 层在插入时使用，不作为业务主键（实际上确实是主键）
    id: str = Field(default_factory=lambda: f"message-{uuid.uuid4().hex}")

    # 消息角色（枚举）
    role: MessageRole

    # 消息内容，保证为字符串或 None（单轮对话中可能为 None，如纯工具调用消息）
    # 落盘时不做 JSON 类型猜测，直接存储字符串，回放时按原样使用
    content: str | None = None

    # 工具调用列表（仅 assistant 消息携带），每个元素为字典，结构如：
    # {"id": "call_xxx", "type": "function", "function": {"name": "...", "arguments": "..."}}
    # 由 LLM 生成后原样序列化为 JSON 存储。
    tool_calls: list[dict] = Field(default_factory=list)

    # 工具调用 ID，用于 role="tool" 的消息，关联到对应的 assistant 工具调用。
    # 空字符串或 None 表示非 tool 消息或无关联。
    tool_call_id: str | None = None

    # ---------- 审计与轨迹追踪字段 ----------
    # 这些字段由上层执行引擎（如 Agent 循环）赋值，用于跨步骤/跨运行追踪。
    step_id: str | None = None       # 工作流中的步骤标识
    run_id: str | None = None        # 单次运行标识（一组 step 的集合）
    otid: str | None = None          # 开放追踪 ID（如 OpenTelemetry trace ID）

    # 消息创建时间（UTC），由 ORM 层在插入时自动填充。
    # 若调用方未提供，则在 insert_message 中取当前时间。
    created_at: datetime | None = None