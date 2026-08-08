# paperflow/core/memory/dream.py
"""Dream 记忆归档后台：在交互式会话每轮循环的间隙被调用一次。

处理流程：按类型白名单过滤待消费的历史记录 → 让模型输出一批编辑指令
（DreamEditBatch）→ 先全量预验证、再逐条落盘 → Git 提交 → 推进消费游标。
"""
import json
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from paperflow.core.memory.memory_index import MemoryIndex

#: 可消费类型白名单：纯监控类记录（如 structured_output）不参与归档，避免噪音
DREAM_CONSUMABLE_TYPES = frozenset({"tool", "reading", "summary", "raw", "intent"})


class DreamEdit(BaseModel):
    """一条记忆文件编辑指令：指定目标文件、操作类型与内容。"""

    file: str                       # 目标记忆文件名（受白名单与路径逃逸校验约束）
    action: Literal["append", "replace", "delete"]   # 追加 / 整体替换 / 删除
    content: str = Field(default="", max_length=8000)
    hook: str = Field(default="", max_length=500)    # 用于更新索引行的简短说明


class DreamEditBatch(BaseModel):
    """模型一次输出的一批编辑指令（上限 20 条）。"""

    edits: list[DreamEdit] = Field(default_factory=list, max_length=20)


#: 路径白名单：只允许 MEMORY.md 与 user_role/feedback_*/project_*/reference_* 顶层 .md
_EDIT_FILE_PATTERN = re.compile(
    r"^(MEMORY|user_role|feedback_[A-Za-z0-9_]+|project_[A-Za-z0-9_]+|reference_[A-Za-z0-9_]+)\.md\Z"
)


class Dream:
    """记忆归档器：在交互式会话每轮循环的间隙被调用，把积累的历史整理进记忆文件。"""

    def __init__(self, store, git, llm, structured,
                 max_entries: int = 20, min_interval_s: float = 60.0):
        self.store = store
        self.git = git
        self.llm = llm
        self.structured = structured
        self.max_entries = max_entries
        self.min_interval_s = min_interval_s
        self._running = False
        # 初始化为当前时刻：进程刚启动视为"刚运行过"，min_interval 内不空转
        self._last_run = time.monotonic()
        self._failures = 0

    async def run_once_if_due(self) -> None:
        """快速判定本次是否该运行归档；全部是廉价检查，绝大多数调用会立即返回。

        三重防线：正在运行中、没有未消费记录、距上次运行不足最小间隔时直接返回。
        """
        if self._running:
            return
        if self.store.read_unprocessed_count() == 0:
            return
        if time.monotonic() - self._last_run < self.min_interval_s:
            return
        self._running = True
        try:
            await self._run_once()
        finally:
            self._running = False
            self._last_run = time.monotonic()   # 每次实际运行后更新（失败重试也计入间隔）

    async def _run_once(self) -> None:
        """执行一次归档：读候选 → 生成编辑指令 → 全量预验证 → 逐条应用 → 提交并推进游标。"""
        entries, max_cursor = self._read_dream_entries(self.max_entries)
        if not entries:
            # 纯监控类型堆积：即使跳过也要推进游标，避免空转重读
            self.store.advance_dream_cursor(max_cursor)
            return
        prompt = self._build_prompt(entries)
        try:
            batch = await self.structured.extract(
                prompt=prompt,
                schema=DreamEditBatch,
                fallback=lambda: DreamEditBatch(edits=[]),
            )
            # 阶段 1：全量预验证——任一非法则整体失败，一条都不写
            for edit in batch.edits:
                self._validate_edit_path(edit.file)
                if edit.action == "delete" and edit.file == "MEMORY.md":
                    raise ValueError("不允许删除 MEMORY.md 自身")
            # 阶段 2：全量通过后逐条应用
            for edit in batch.edits:
                self._apply_edit(edit)
            self.git.commit(f"dream: {len(entries)} 条历史")
            self.store.advance_dream_cursor(max_cursor)
            self._failures = 0
        except Exception:
            self._failures += 1
            if self._failures >= 3:
                self.store.advance_dream_cursor(max_cursor)

    def _build_prompt(self, entries: list[dict]) -> str:
        """注入约束：MEMORY.md 索引上限 200 行，追加时合并/淘汰旧条目。"""
        parts = [
            "你是 paperFlow 的记忆归档器。分析以下历史记录，输出编辑指令更新记忆文件。",
            f"MEMORY.md 索引上限 {MemoryIndex.MAX_LINES} 行——追加索引行时必须合并或淘汰旧条目。",
            "记忆文件：MEMORY.md（索引）、user_role.md、feedback_*.md、project_*.md、reference_*.md。",
            "",
            "历史记录：",
        ]
        for e in entries:
            parts.append(json.dumps(e, ensure_ascii=False))
        return "\n".join(parts)

    def _read_dream_entries(self, limit: int) -> tuple[list[dict], int]:
        """按白名单过滤历史记录，并决定消费游标应推进到哪里。

        正常情况下游标推进到本次已消费的最后一条，不越过未消费的条目——否则超过
        上限的待处理记录永远不会被归档。当剩余全是损坏行（半写崩溃残留）时，推进到
        已见 cursor 的最大值。游标单调递增、绝不倒退：倒退会让已处理过的条目被再次
        归档，append 类编辑会被重复写入。
        """
        all_entries = self.store.read_unprocessed_history()
        if not all_entries:
            return [], self.store._read_cursor()
        filtered = [e for e in all_entries if e.get("type") in DREAM_CONSUMABLE_TYPES]
        consumed = filtered[:limit]
        if not consumed:
            return [], all_entries[-1]["cursor"]    # 纯监控：推进到已检查位置
        return consumed, consumed[-1]["cursor"]     # 推进到最后消费条目的 cursor

    def _apply_edit(self, edit: DreamEdit) -> None:
        """把单条编辑指令落到文件系统：删除/追加/替换，并视需要同步 MEMORY.md 索引。"""
        path = self._validate_edit_path(edit.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if edit.action == "delete":
            if edit.file == "MEMORY.md":
                raise ValueError("不允许删除 MEMORY.md 自身")
            path.unlink(missing_ok=True)
            self._sync_index(edit.file, None)
            return
        if edit.action == "append":
            with open(path, "a", encoding="utf-8") as f:
                f.write(edit.content + "\n")
        elif edit.action == "replace":
            with open(path, "w", encoding="utf-8") as f:
                f.write(edit.content)
        if edit.file != "MEMORY.md":
            self._sync_index(edit.file, edit.hook)

    def _validate_edit_path(self, file: str) -> Path:
        """校验编辑目标符合文件名白名单且不逃逸 memory 目录，返回解析后的绝对路径。"""
        if not _EDIT_FILE_PATTERN.match(file):
            raise ValueError(f"非法编辑目标（不在白名单）: {file}")
        path = (self.store.memory_dir / file).resolve()
        if not path.is_relative_to(self.store.memory_dir.resolve()):
            raise ValueError(f"编辑路径逃逸 memory 目录: {file}")
        return path

    def _sync_index(self, file: str, hook: str | None) -> None:
        """更新 MEMORY.md 索引：移除旧的对应条目，hook 非空时追加一条新的索引行。"""
        index_path = self.store.memory_dir / "MEMORY.md"
        lines = index_path.read_text(encoding="utf-8").splitlines() if index_path.exists() else []
        target = f"[{file}]"
        remaining = [ln for ln in lines if target not in ln]
        if hook:
            remaining.append(f"- [{file}]({file}) — {hook}")
        index_path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
