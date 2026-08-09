"""Letta Block / BaseBlock 数据模型（命名对齐 letta/schemas/block.py）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["BaseBlock", "Block"]


def _block_id() -> str:
    import uuid
    return f"block-{uuid.uuid4().hex}"


class BaseBlock(BaseModel):
    value: str = ""
    limit: int = 2000                      # 字符上限，超限报 Exceeds {limit} character limit
    label: str | None = None
    description: str | None = None
    metadata_: dict[str, Any] = Field(default_factory=dict)   # API 层暴露为 metadata
    read_only: bool = False
    is_template: bool = False
    template_name: str | None = None
    hidden: bool | None = None


class Block(BaseBlock):
    id: str = Field(default_factory=_block_id)
    version: int = 1                    # 乐观锁计数：DB 列、由 orm/BlockManager 读写
    project_id: str | None = None
    organization_id: str | None = None
    created_by_id: str | None = None
    last_updated_by_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def human(cls, value: str) -> "Block":
        return cls(label="human", value=value)

    @classmethod
    def persona(cls, value: str) -> "Block":
        return cls(label="persona", value=value)

    @classmethod
    def new(cls, label: str, value: str) -> "Block":
        return cls(label=label, value=value)
