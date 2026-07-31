# paperflow/core/memory/dream.py
"""Dream 后台：CLI 每轮循环间隙调用 run_once_if_due()。

白名单过滤消费历史 → StructuredOutput 输出 DreamEditBatch →
两阶段应用（全量预验证再逐条写）→ GitStore commit → 游标推进。
"""
import json
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from paperflow.core.memory.memory_index import MemoryIndex

#: 可消费类型白名单：纯监控类型（structured_output 等）不进入 prompt
DREAM_CONSUMABLE_TYPES = frozenset({"tool", "reading", "summary", "raw", "intent"})


class DreamEdit(BaseModel):
    file: str
    action: Literal["append", "replace", "delete"]
    content: str = Field(default="", max_length=8000)
    hook: str = Field(default="", max_length=500)


class DreamEditBatch(BaseModel):
    edits: list[DreamEdit] = Field(default_factory=list, max_length=20)


#: 路径白名单：只允许 MEMORY.md 与 user_role/feedback_*/project_*/reference_* 顶层 .md
_EDIT_FILE_PATTERN = re.compile(
    r"^(MEMORY|user_role|feedback_[A-Za-z0-9_]+|project_[A-Za-z0-9_]+|reference_[A-Za-z0-9_]+)\.md$"
)


class Dream:
    """CLI 每轮循环间隙调用 run_once_if_due()。"""

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
        """快速检查（无 LLM）→ 大多立即返回。"""
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

    async def _run_once(self) -> None:
        entries, max_cursor = self._read_dream_entries(self.max_entries)
        if not entries:
            # 纯监控类型堆积：跳过也推进游标，避免空转重读
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
        """白名单过滤。游标推进必须用原始最大 cursor（含被过滤的监控条目）。"""
        all_entries = self.store.read_unprocessed_history()
        if not all_entries:
            return [], 0
        filtered = [e for e in all_entries if e.get("type") in DREAM_CONSUMABLE_TYPES]
        return filtered[:limit], all_entries[-1]["cursor"]

    def _apply_edit(self, edit: DreamEdit) -> None:
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
        if not _EDIT_FILE_PATTERN.match(file):
            raise ValueError(f"非法编辑目标（不在白名单）: {file}")
        path = (self.store.memory_dir / file).resolve()
        if not path.is_relative_to(self.store.memory_dir.resolve()):
            raise ValueError(f"编辑路径逃逸 memory 目录: {file}")
        return path

    def _sync_index(self, file: str, hook: str | None) -> None:
        index_path = self.store.memory_dir / "MEMORY.md"
        lines = index_path.read_text(encoding="utf-8").splitlines() if index_path.exists() else []
        target = f"[{file}]"
        remaining = [ln for ln in lines if target not in ln]
        if hook:
            remaining.append(f"- [{file}]({file}) — {hook}")
        index_path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
