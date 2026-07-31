import json
import threading
import time
from datetime import datetime
from pathlib import Path

from paperflow.core.security import (
    SecurityMiddleware, ToolContext, PolicyDenied, SecurityBlocked, ConfirmRequired,
)


class MemoryStore:
    """history.jsonl 追加 + 游标管理 + compact。"""

    def __init__(self, memory_dir: Path, max_history_entries: int = 1000):
        self.memory_dir = Path(memory_dir)
        self.history_path = self.memory_dir / "history.jsonl"
        self.cursor_path = self.memory_dir / ".cursor"
        self.dream_cursor_path = self.memory_dir / ".dream_cursor"
        self.max_history_entries = max_history_entries
        self._lock = threading.Lock()       # 游标原子性（Layer 4 parallel_spawn 并发就绪）

    def append_history(self, entry: dict) -> int:
        """追加一条记录，返回 cursor。同步写（单行 JSON，微秒级）。"""
        with self._lock:
            cursor = self._read_cursor() + 1
            self._write_cursor(cursor)
            record = {
                **entry,                    # 调用方字段在前
                "cursor": cursor,           # 内部字段在后——绝不被 entry 覆盖
                "timestamp": datetime.now().isoformat(),
            }
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return cursor

    def read_unprocessed_history(self, since: int | None = None,
                                 limit: int | None = None) -> list[dict]:
        """返回 cursor > since 的条目（含所有 type，过滤由消费方决定）。
        since=None 时默认从 .dream_cursor 文件读取。
        limit 限制返回条数。坏行容错：半写 JSON 跳过不阻断。"""
        if since is None:
            since = self._read_dream_cursor()
        entries = []
        if not self.history_path.exists():
            return entries
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue                    # 坏行跳过（半写崩溃残留），不阻断
            if entry.get("cursor", 0) > since:
                entries.append(entry)
        if limit is not None:
            return entries[:limit]
        return entries

    def read_unprocessed_count(self) -> int:
        """快速检查：.dream_cursor 与 .cursor 差值。缺失/损坏文件按 0 处理，不抛异常。"""
        try:
            return max(0, self._read_cursor() - self._read_dream_cursor())
        except Exception:
            return 0

    def advance_dream_cursor(self, cursor: int) -> None:
        """Dream 处理后推进。语义 = 已检查到该位置（含未消费类型）。"""
        with self._lock:
            self.dream_cursor_path.write_text(str(cursor), encoding="utf-8")

    def compact_history(self) -> None:
        """保留最近 max_history_entries 条，不丢弃 .dream_cursor 未处理的条目。
        必须持有 self._lock——重写整个文件与 append 互斥。"""
        with self._lock:
            if not self.history_path.exists():
                return
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= self.max_history_entries:
                return
            entries = []
            for line in lines:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            last_dream = self._read_dream_cursor()
            # 第一个未处理条目的位置
            first_unprocessed = next(
                (i for i, e in enumerate(entries) if e.get("cursor", 0) > last_dream),
                len(entries),
            )
            keep_from = min(len(entries) - self.max_history_entries, first_unprocessed)
            kept = entries[keep_from:]
            with open(self.history_path, "w", encoding="utf-8") as f:
                for e in kept:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def _read_cursor(self) -> int:
        if not self.cursor_path.exists():
            return 0
        try:
            return int(self.cursor_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return 0

    def _write_cursor(self, cursor: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(cursor), encoding="utf-8")

    def _read_dream_cursor(self) -> int:
        if not self.dream_cursor_path.exists():
            return 0
        try:
            return int(self.dream_cursor_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return 0


def _error_type(error: Exception | None) -> str:
    if error is None:
        return ""
    if isinstance(error, PolicyDenied):     return "policy_denied"
    if isinstance(error, SecurityBlocked):  return "security_blocked"
    if isinstance(error, ConfirmRequired):  return "user_denied"
    return "exec_error"


class ExperienceMemoryMiddleware(SecurityMiddleware):
    """管道最后一个中间件（第 ⑤ 个）：after 阶段记录工具调用。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    async def after(self, ctx: ToolContext) -> None:
        if ctx.tool is None:
            # 未知工具/解析失败：由 Audit 覆盖（含幻觉工具名），Experience 只学真实工具经验
            return
        self.store.append_history({
            "type": "tool",
            "tool_name": ctx.tool_name,
            "success": ctx.error is None,
            "duration_ms": int((time.monotonic() - (ctx.started_at or 0.0)) * 1000),
            "error_type": _error_type(ctx.error),
            "summary": ctx.result.summary if ctx.result else {},
        })
