"""Sleeptime 记忆整合后台（取代 Dream，对齐 Letta sleeptime compute）。

CLI REPL 每轮循环顶部 run_once_if_due()。读取未消费历史 → LLM 用
BASE_SLEEPTIME_TOOLS 语义输出编辑指令（memory_replace/memory_insert/
memory_rethink）→ 经 BlockManager 应用 → 结束（memory_finish_edits 语义）。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

__all__ = ["Sleeptime", "MemoryEditBatch", "MemoryEdit"]

#: 可写记忆文件白名单（LLM 输出不可信，写入前必须校验）
_EDIT_FILE_PATTERN = re.compile(
    r"^(system/[A-Za-z0-9_]+|[A-Za-z0-9_]+|feedback_[A-Za-z0-9_]+|"
    r"project_[A-Za-z0-9_]+|reference_[A-Za-z0-9_]+)\.md\Z")


class MemoryEdit(BaseModel):
    file: str
    action: Literal["append", "replace", "delete"]
    content: str = Field(default="", max_length=8000)
    hook: str = Field(default="", max_length=500)


class MemoryEditBatch(BaseModel):
    edits: list[MemoryEdit] = Field(default_factory=list, max_length=20)


class Sleeptime:
    def __init__(self, agent_state, block_manager, passage_manager, message_manager,
                 structured, enable: bool = False, frequency: int = 50,
                 min_interval_s: float = 60.0, max_entries: int = 20):
        self.agent_state = agent_state
        self.block_manager = block_manager
        self.passage_manager = passage_manager
        self.message_manager = message_manager
        self.structured = structured
        self.enable = enable
        self.frequency = frequency
        self.min_interval_s = min_interval_s
        self.max_entries = max_entries
        self._running = False
        self._last_run = time.monotonic()
        self._failures = 0
        #: 已处理到的消息数量游标（基于 messages 表行数，从 DB 推导）
        self._cursor = self.message_manager.size(agent_state.agent_id) if message_manager else 0

    async def run_once_if_due(self) -> None:
        """快速判定是否该运行整合；全部廉价检查，大多立即返回。"""
        if not self.enable or self._running:
            return
        size = self.message_manager.size(self.agent_state.agent_id)
        if size - self._cursor < self.frequency:
            return
        if time.monotonic() - self._last_run < self.min_interval_s:
            return
        self._running = True
        try:
            await self._run_once()
        finally:
            self._running = False
            self._last_run = time.monotonic()

    async def _run_once(self) -> None:
        """读取新消息 → LLM 编辑指令 → 全量预验证 → 逐条应用 → commit → 推进。"""
        new_msgs = self.message_manager.get_messages_by_agent_id(
            self.agent_state.agent_id)
        new_msgs = new_msgs[self._cursor:]
        if not new_msgs:
            self._cursor = self.message_manager.size(self.agent_state.agent_id)
            return
        prompt = self._build_prompt(new_msgs)
        try:
            batch = await self.structured.extract(
                prompt=prompt, schema=MemoryEditBatch,
                fallback=lambda: MemoryEditBatch(edits=[]))
            # 阶段 1：全量预验证——任一非法则整体失败，一条都不写。
            # 校验失败（ValueError）上抛给调用方，不计入连败计数
            for edit in batch.edits:
                self._validate_edit(edit)
            # 阶段 2：全量通过后逐条应用（映射到 block 编辑）
            for edit in batch.edits:
                self._apply_edit(edit)
            if self.block_manager.memfs is not None:
                self.block_manager._commit(f"sleeptime: {len(new_msgs)} 条历史")
            self._cursor = self.message_manager.size(self.agent_state.agent_id)
            self._failures = 0
        except ValueError:
            raise    # 原子性失败：非法编辑目标不吞掉，直接暴露
        except Exception:
            self._failures += 1
            if self._failures >= 3:
                # 防卡死：连败 3 次强制前进
                self._cursor = self.message_manager.size(self.agent_state.agent_id)

    def _build_prompt(self, new_msgs: list) -> str:
        parts = [
            "你是 paperFlow 的记忆整合器（sleeptime）。分析以下新对话，输出记忆编辑指令。",
            "可编辑：feedback_*.md / project_*.md / reference_*.md / system/*.md。",
            "规则：值得长期记住才写；合并重复；旧条目被推翻时 replace 为新结论。",
            "", "新对话：",
        ]
        for m in new_msgs:
            parts.append(f"[{m.role.value}] {m.content}")
        return "\n".join(parts)

    def _validate_edit(self, edit: MemoryEdit) -> None:
        if not _EDIT_FILE_PATTERN.match(edit.file):
            raise ValueError(f"非法编辑目标: {edit.file}")
        if edit.action == "delete" and edit.file.startswith("system/"):
            raise ValueError("不允许删除 system/ 块")

    def _apply_edit(self, edit: MemoryEdit) -> None:
        """把编辑指令映射到 BlockManager（file → block label）。

        追加/替换的目标块不存在时创建（对应 Dream 的 open "a"/"w" 即新建语义）；
        删除只作用于已存在块。
        """
        label = edit.file.removesuffix(".md").replace("system/", "")
        if edit.action == "delete":
            b = self.block_manager.get_block_by_label(label)
            if b is not None:
                self.block_manager.delete_block(b.id)
            return
        b = self.block_manager.get_block_by_label(label)
        if edit.action == "append":
            if b is None:
                self.block_manager.create_block(label, edit.content)
            else:
                self.block_manager.update_block_value(
                    label, b.value + "\n" + edit.content)
        elif edit.action == "replace":
            if b is None:
                self.block_manager.create_block(label, edit.content)
            else:
                self.block_manager.update_block_value(label, edit.content)
