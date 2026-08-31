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
    """passage 的内容与检索元数据字段（不含持久化标识）。

    此类定义 Passage 的业务数据部分，供 API 传输或服务层使用，与持久化字段分离。

    Attributes:
        text: 长期记忆的文本内容（核心信息载体），必填。
        embedding: 文本的语义向量（浮点数列表），由 embedder 生成。
                  若为 None，则检索时该 passage 不参与语义排序，仅靠标签/时间过滤。
        metadata_: 扩展元数据（dict），可存储来源 URL、作者、日期等附加信息。
                   API 层通常暴露为 "metadata"。
        tags: 标签列表，用于分类和过滤（例如 "arxiv", "paper", "important"）。
              检索支持“全匹配”标签过滤（AND 逻辑）。
    """

    text: str
    embedding: list[float] | None = None
    metadata_: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class Passage(PassageBase):
    """持久化 passage：在 PassageBase 之上加 id / 时间戳 / 归属字段。

    is_deleted 支持软删（保留行以便审计）；source_id / file_id / archive_id
    记录来源与归档归属。

    设计考虑：
        - 软删除保留数据行，便于审计恢复及追溯历史。
        - 归属字段（agent_id / source_id / file_id / archive_id）为多租户、
          溯源和关联归档提供可扩展性。
        - id 使用 "passage-" 前缀 + uuid hex，确保全局唯一且可读。
        - created_at 由 ORM 在插入时自动填充（当前 UTC 时间）。
    """

    id: str = Field(default_factory=lambda: f"passage-{uuid.uuid4().hex}")
    # 创建时间（UTC），在 insertion 时自动设为当前时间
    created_at: datetime | None = None

    # 所属 agent 标识（用于隔离不同 agent 的记忆）
    agent_id: str | None = None

    # 可选的来源标识（如原文链接、导入文件 ID 等）
    source_id: str | None = None
    file_id: str | None = None

    # 若 passage 属于某个归档，记录归档 ID（多对多关系通过 ArchiveManager 维护）
    archive_id: str | None = None

    # 软删除标志：为 True 时表示已删除，但数据行仍保留（默认 False）
    is_deleted: bool = False