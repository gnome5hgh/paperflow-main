"""AgentState：agent 生命周期状态模型（agent_state 表的一行 JSON 快照）。

记录 agent 的元信息、当前记忆块容器与 in-context 窗口的消息 id 列表。
message_ids 是「当前窗口」的持久化——压缩后被驱逐的旧消息只移出该列表、
不删 messages 表行，Recall 完整可追溯。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from paperflow.core.memory.schemas.memory import Memory

__all__ = ["AgentState"]


class AgentState(BaseModel):
    """Agent 状态模型：内存态与持久化字段的联合体。

    两个关键设计：
        1. `memory` 字段不持久化到 agent_state 表——每次通过 AgentManager.get_agent()
           从 block_manager 动态构建，保证始终反映块表最新状态。
        2. `message_ids` 持久化到数据库，是「当前 in-context 窗口」的显式记录；
           压缩时只从该列表移除旧 id，不物理删除 messages 表行（Recall 可追溯全部历史）。
    """

    # 允许 Memory 容器类（普通 Python 类，非 Pydantic 模型）作为字段类型
    # 若不设置，Pydantic 会尝试递归校验并抛出异常
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ---------- 标识与元数据 ----------
    agent_id: str                                    # 唯一标识符（主键）
    name: str | None = None                          # 可读名称（可选）
    description: str | None = None                  # 描述（可选，用于展示/索引）
    system: str | None = None                       # 系统提示词（可选，覆盖默认）
    model: str | None = None                        # 指定模型名称（可选）

    # ---------- 记忆与上下文 ----------
    # 核心记忆容器（assistant/profile 等块）。不持久化到 agent_state 表，由 AgentManager 动态构建。
    # 使用 default_factory 确保每个 AgentState 实例拥有独立的 Memory 容器，
    # 避免多个实例共享同一可变对象的默认值陷阱。
    memory: Memory = Field(default_factory=lambda: Memory(blocks=[]))

    # 工具列表（仅运行时持有，不持久化，由 Agent 系统注入）
    tools: list[Any] = Field(default_factory=list)

    # 上下文窗口大小限制（可选，用于触发压缩）
    context_window_limit: int | None = None

    # ---------- 窗口控制 ----------
    # 当前 in-context 窗口的消息 ID 列表（顺序即为对话顺序）。
    # 持久化到 DB 的 JSON 字段，压缩时移出旧 id，消息行本身不删除。
    # 若列表为空，则回退为加载全部持久化消息（首次运行或未压缩状态）。
    message_ids: list[str] = Field(default_factory=list)

    # ---------- 审计 ----------
    created_at: datetime | None = None              # 创建时间（由 DB 或调用方赋值）