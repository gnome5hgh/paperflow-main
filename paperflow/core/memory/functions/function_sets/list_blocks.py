# paperflow/core/memory/functions/function_sets/list_blocks.py
"""清单块工具：未读清单 / 浏览历史的确定性增删。

两个清单共用同一套「给块追加行 / 按 key 删行 / 块缺失先建」逻辑，
抽成 ListBlockTool 基类，子类只声明 name/description/parameters 与条目格式。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.services.tool_manager import MemoryToolsContext


class ListBlockTool(Tool):
    """清单块工具基类：block_label 指定目标块，子类定义条目格式。

    append 语义=只追加不改旧（读旧值 + 拼新行）；块缺失时 create_block
    （create_block → sync_block_to_file → regenerate_index，新文件自动进
    memory_filesystem.md 索引）。remove 按 key 精确匹配行（含该 key 的行），
    找不到返回明确错误，不静默。
    """

    block_label = ""

    def __init__(self, ctx: MemoryToolsContext):
        self._ctx = ctx

    def _bm(self):
        return self._ctx.block_manager

    def _ensure_block(self) -> None:
        """目标块缺失时创建（自动更新 memory_filesystem.md 索引）。"""
        if self._bm().get_block_by_label(self.block_label) is None:
            self._bm().create_block(self.block_label, "")

    def _format_entry(self, **kwargs) -> str:
        """子类实现：把参数格式化为一行条目。"""
        raise NotImplementedError

    def append_line(self, **kwargs) -> str:
        self._ensure_block()
        line = self._format_entry(**kwargs)
        block = self._bm().get_block_by_label(self.block_label)
        value = block.value.strip()
        new_value = f"{value}\n{line}" if value else line
        self._bm().update_block_value(self.block_label, new_value)
        return f"Appended to {self.block_label}"

    def remove_line_by_key(self, key: str) -> str:
        self._ensure_block()
        block = self._bm().get_block_by_label(self.block_label)
        lines = [ln for ln in block.value.splitlines() if ln.strip()]
        kept = [ln for ln in lines if key not in ln]
        if len(kept) == len(lines):
            return f"Error: '{key}' not found in {self.block_label}"
        self._bm().update_block_value(self.block_label, "\n".join(kept))
        return f"Removed from {self.block_label}"

    def execute(self, action: str = "append", **kwargs) -> ToolResult:
        """统一入口：默认 append 追加一行；action="remove" 按 key 删行。

        remove 的 key 取 kwargs 里第一个参数值——子类条目格式里标识条目的
        字段通常排 parameters 首位（如 title）。删不到时 remove_line_by_key
        返回带 "not found" 的错误文本，不抛异常。
        """
        if action == "remove":
            key = next(iter(kwargs.values()))
            return ToolResult(text=self.remove_line_by_key(str(key)))
        return ToolResult(text=self.append_line(**kwargs))
