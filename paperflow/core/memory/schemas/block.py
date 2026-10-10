"""核心记忆块数据模型：BaseBlock（块内容与元数据）+ Block（含持久化字段）。

一个 Block 就是一段「可被 LLM 编辑的命名记忆」——label 是名字（assistant、profile、unread_list、history_list等），value 是内容（文件内容），
limit 是长度上限，read_only 表示保护块（不可改/删）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["BaseBlock", "Block"]


def _block_id() -> str:
    """生成块唯一 id（block- 前缀 + uuid hex）。

    Returns:
        格式为 "block-<32位hex>" 的字符串，例如 "block-a1b2c3d4e5f6..."。
    """
    import uuid
    return f"block-{uuid.uuid4().hex}"


class BaseBlock(BaseModel):
    """块内容与元数据字段（不含持久化标识）。

    这是块的“业务数据”部分，被 Block 继承以添加数据库持久化字段（id、version、时间戳等）。
    设计上分离便于在不需要 DB 关联的场景（如 API 传输、模板）中复用。

    Attributes:
        value: 块的核心文本内容（记忆主体）。
        limit: 字符长度上限（默认 2000），更新时超限将抛出 ValueError。
        label: 块的名称标签（如 "assistant", "profile", "unread_list"），
               通常作为唯一业务标识，但并非严格主键（主键为 id）。
        description: 可读描述（用于展示或索引，不参与逻辑）。
        metadata_: 可选的扩展元数据（dict），API 层暴露为 "metadata"。
                 下划线后缀是为了避免与 Pydantic 保留字段名冲突。
        read_only: 若为 True，则块不可更新/删除（保护块，如系统预置核心块）。
        is_template: 标记该块是否为模板（仅供前端或 AI 辅助编辑）。
        template_name: 当 is_template=True 时，模板的名称。
        hidden: 是否在索引或列表中隐藏（用于内部块，不向 LLM 暴露）。
    """

    value: str = ""
    limit: int = 2000                      # 字符上限，超限抛 BlockLimitExceeded（见 common/errors.py）
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

    说明：
        - 单用户 + 全局写锁场景下，并发写已被串行化，因此 version 不作为 CAS 比较字段，
          仅用于记录变更次数和排序历史快照。
        - project_id / organization_id / created_by_id / last_updated_by_id 为预留字段，
          供未来多租户或权限追踪使用，当前均置为 None。
        - created_at / updated_at 由 ORM 层在插入/更新时自动填充（若未提供）。

    Attributes:
        id: str，主键（"block-<hex>" 自动生成）
        version: int，乐观锁计数；每次更新 +1，写前快照进 block_history 作撤销/重做链
        project_id: str | None，预留：项目归属
        organization_id: str | None，预留：组织归属
        created_by_id: str | None，预留：创建者 ID
        last_updated_by_id: str | None，预留：最后更新者 ID
        created_at: datetime | None，创建时间（由 DB/ORM 填充）
        updated_at: datetime | None，最后更新时间（由 DB/ORM 填充）
    """

    id: str = Field(default_factory=_block_id)           # 主键，自动生成
    version: int = 1                                    # 乐观锁计数：DB 列、由 orm/BlockManager 读写
    project_id: str | None = None                       # 预留：项目归属
    organization_id: str | None = None                  # 预留：组织归属
    created_by_id: str | None = None                    # 预留：创建者 ID
    last_updated_by_id: str | None = None               # 预留：最后更新者 ID
    created_at: datetime | None = None                  # 创建时间（由 DB 或 ORM 填充）
    updated_at: datetime | None = None                  # 最后更新时间（由 DB 或 ORM 填充）

    @classmethod
    def profile(cls, value: str) -> "Block":
        """构造 label=profile 的块（用户画像块，MemoryConsolidator 定向写入目标）。

        Args:
            value: 用户画像文本内容。

        Returns:
            一个 label 固定为 "profile" 的 Block 实例。

        用途：
            - MemoryConsolidator 过程将用户身份/偏好/背景持续写入此块。
            - 与 assistant 块共同构成 Memory.compile() 的常驻 system 内容。
        """
        return cls(label="profile", value=value)

    @classmethod
    def assistant(cls, value: str) -> "Block":
        """构造 label=assistant 的块（助手自我认知块，可演进）。

        Args:
            value: 助手工作方式/自我认知文本。

        Returns:
            一个 label 固定为 "assistant" 的 Block 实例。

        用途：
            - 记录与用户协作中学到的角色调整与工作方式偏好（区别于
              各子 agent 静态的 AGENT.md 系统提示）。
            - MemoryConsolidator 以 replace 整块重写的方式维护。
        """
        return cls(label="assistant", value=value)

    @classmethod
    def new(cls, label: str, value: str) -> "Block":
        """构造一个指定 label/value 的新块。

        Args:
            label: 块的标签名称。
            value: 块的内容文本。

        Returns:
            一个 Block 实例，其余字段（limit、description 等）使用默认值。

        便捷工厂方法，用于快速创建普通块（非 assistant/profile 专用）。
        """
        return cls(label=label, value=value)