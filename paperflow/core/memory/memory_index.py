"""MEMORY.md 索引加载器。

MemoryIndex 在每轮对话执行前重新读取 MEMORY.md，让归档环节对索引的修改能立即在
后续对话中生效；读取时按行数上限截断，避免超长索引占满上下文。
"""
import sys
from pathlib import Path


class MemoryIndex:
    """MEMORY.md 索引加载器：每轮对话执行时重新读取，让归档写入的索引即时生效。"""

    MAX_LINES = 200              # 索引超过 200 行时截断（在读取侧截断，避免超长索引挤占上下文）

    def __init__(self, memory_dir: Path):
        """记录索引文件的路径。"""
        self.index_path = Path(memory_dir) / "MEMORY.md"

    async def read(self) -> str:
        """读取索引内容；文件不存在或损坏时返回空串并打印告警，不中断对话执行。"""
        if not self.index_path.exists():
            return ""
        try:
            lines = self.index_path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError) as e:
            print(f"[memory] MEMORY.md 读取失败: {e}", file=sys.stderr)
            return ""
        return "\n".join(lines[: self.MAX_LINES])
