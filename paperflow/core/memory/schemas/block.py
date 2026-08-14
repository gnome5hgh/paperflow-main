"""核心记忆块数据模型：BaseBlock（块内容与元数据）+ Block（含持久化字段）。

一个 Block 就是一段「可被 LLM 编辑的命名记忆」——label 是名字，value 是内容，
limit 是长度上限，read_only 表示保护块（不可改/删）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["BaseBlock", "Block"]


def _block_id() -> str:
    """生成块唯一 id（block- 前缀 + uuid hex）。"""
    import uuid
    return f"block-{uuid.uuid4().hex}"


class BaseBlock(BaseModel):
    """块内容与元数据字段（不含持久化标识）。"""

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
    """持久化块：在 BaseBlock 之上加 id / 版本号 / 时间戳等 DB 字段。

    version 是乐观锁计数：由 orm/BlockManager 每次更新时 +1，写前快照进
    block_history 作撤销/重做链；它只做写入标注与历史排序，不做并发比较。
    """

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
        """构造 label=human 的块（用户画像块，Sleeptime 定向写入目标）。"""
        return cls(label="human", value=value)

    @classmethod
    def persona(cls, value: str) -> "Block":
        """构造 label=persona 的块（助手身份块，可自我演进）。"""
        return cls(label="persona", value=value)

    @classmethod
    def new(cls, label: str, value: str) -> "Block":
        """构造一个指定 label/value 的新块。"""
        return cls(label=label, value=value)
