"""archival 长期记忆模型：Passage / PassageBase。

Passage 是超出核心块容量的长期知识单元，落 archival_passages 表；embedding
字段存语义向量（可选，None 时退化为标签/时间过滤检索）。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Passage", "PassageBase"]


class PassageBase(BaseModel):
    """passage 的内容与检索元数据字段（不含持久化标识）。"""

    text: str
    embedding: list[float] | None = None
    metadata_: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class Passage(PassageBase):
    """持久化 passage：在 PassageBase 之上加 id / 时间戳 / 归属字段。

    is_deleted 支持软删（保留行以便审计）；source_id / file_id / archive_id
    记录来源与归档归属。
    """

    id: str = Field(default_factory=lambda: f"passage-{uuid.uuid4().hex}")
    created_at: datetime | None = None
    agent_id: str | None = None
    source_id: str | None = None
    file_id: str | None = None
    archive_id: str | None = None
    is_deleted: bool = False
