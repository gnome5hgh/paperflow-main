"""Tool 领域实体：工具这一业务里「一个东西」的模型。

`Tool` 抽象基类与它的安全元数据字段（风险档、副作用、来源、语义根提示），以及
由副作用推出的写入者判定（`is_writer` / `WRITE_EFFECTS`）。与 `dto/` 的分界是
「是不是这个业务里的东西本身」——实体回答「系统里有什么」，dto 只负责把数据从
一层搬到另一层。
"""
from paperflow.core.tool.domain.entity.tool import WRITE_EFFECTS, Tool, is_writer

__all__ = ["Tool", "WRITE_EFFECTS", "is_writer"]
