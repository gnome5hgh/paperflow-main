"""Letta Passage / PassageBase 模型，命名对齐 letta/schemas/passage.py。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Passage", "PassageBase"]


class PassageBase(BaseModel):
    text: str
    embedding: list[float] | None = None
    metadata_: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class Passage(PassageBase):
    id: str = Field(default_factory=lambda: f"passage-{uuid.uuid4().hex}")
    created_at: datetime | None = None
    agent_id: str | None = None
    source_id: str | None = None
    file_id: str | None = None
    archive_id: str | None = None
    is_deleted: bool = False
