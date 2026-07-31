import sys
from pathlib import Path


class MemoryIndex:
    """MEMORY.md 索引加载器：每轮 run() 重新读取，Dream 写入即时生效。"""

    MAX_LINES = 200              # ADR 0004：超过 200 行截断（读取侧截断）

    def __init__(self, memory_dir: Path):
        self.index_path = Path(memory_dir) / "MEMORY.md"

    async def read(self) -> str:
        """读取索引内容。文件不存在/损坏 → 返回空串 + 日志告警，不阻断 run()。"""
        if not self.index_path.exists():
            return ""
        try:
            lines = self.index_path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError) as e:
            print(f"[memory] MEMORY.md 读取失败: {e}", file=sys.stderr)
            return ""
        return "\n".join(lines[: self.MAX_LINES])
