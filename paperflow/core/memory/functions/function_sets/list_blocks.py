# paperflow/core/memory/functions/function_sets/list_blocks.py
"""清单块工具：未读清单 / 浏览历史的确定性增删。

两个清单共用同一套「给块追加行（缺失先建）/ 按 key 删行」逻辑，
抽成 ListBlockTool 基类，子类只声明 name/description/parameters 与条目格式。
"""
from datetime import datetime

from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.services.tool_manager import MemoryToolsContext


class ListBlockTool(Tool):
    """清单块工具基类：block_label 指定目标块，子类定义条目格式。

    append 语义=只追加不改旧（读旧值 + 拼新行）；块缺失时 create_block
    （create_block → sync_block_to_file → regenerate_index，新文件自动进
    memory_filesystem.md 索引）。remove 按行首 `- {key}` 前缀匹配行（标题在
    条目开头，比子串匹配更精确），找不到返回明确错误，不静默；块缺失时
    不建块直接返回 not found（remove 是清理动作，不该留下空块残留）。
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
        """按行首 `- {key}` 前缀匹配删行，找不到返回明确错误，不静默。

        局限：只做行首前缀匹配，标题互为前缀时仍可能误删（如 "Attention"
        命中 "Attention Is All You Need"）；权威长标题罕见。块缺失时直接
        返回 not found，不物化空块——remove 只是清理，不该产生残留空块。
        """
        # 空 key 直接拒绝，避免 "- " 前缀误删整块
        if not key.strip():
            return "Error: empty title for removal"
        block = self._bm().get_block_by_label(self.block_label)
        if block is None:
            return f"Error: '{key}' not found in {self.block_label}"
        prefix = f"- {key}"
        lines = [ln for ln in block.value.splitlines() if ln.strip()]
        kept = [ln for ln in lines if not ln.startswith(prefix)]
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
            key = next(iter(kwargs.values()), "")
            return ToolResult(text=self.remove_line_by_key(str(key)))
        return ToolResult(text=self.append_line(**kwargs))


class UnreadListAddTool(ListBlockTool):
    """把论文加入未读清单。title 必须来自提取链/用户（禁文件名）。"""

    name = "unread_list_add"
    block_label = "unread_list"
    description = "把一篇论文加入未读清单，追加 `- 标题 (来源)` 行"
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "论文权威标题（提取链/用户提供，禁文件名）"},
            "source": {"type": "string", "description": "来源：arxiv:ID / openalex / pdf:路径"},
        },
        "required": ["title"],
    }

    def _format_entry(self, title: str, source: str = "") -> str:
        return f"- {title} ({source})" if source else f"- {title}"

    def execute(self, title: str = "", source: str = "") -> ToolResult:
        if not title or not title.strip():
            return ToolResult(text="Error: title is required (extract from paper, not filename)")
        return ToolResult(text=self.append_line(title=title, source=source))


class UnreadListRemoveTool(ListBlockTool):
    """把论文移出未读清单（按权威标题精确删行）。"""

    name = "unread_list_remove"
    block_label = "unread_list"
    description = "把一篇论文移出未读清单，按权威标题删除对应行"
    parameters = {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "要移除的论文权威标题"}},
        "required": ["title"],
    }

    def execute(self, title: str) -> ToolResult:
        return ToolResult(text=self.remove_line_by_key(title))


class HistoryAppendTool(ListBlockTool):
    """把一次论文消费事件追加进浏览历史（只追加不改旧）。

    条目格式 `[{时间}] {action}《{title}》`——同论文可多次追加，靠时间/动作区分。
    """

    name = "history_append"
    block_label = "history_list"
    description = "把一次论文消费事件（精读/写笔记）追加进浏览历史，只追加不改旧"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "动作：精读 / 写笔记"},
            "title": {"type": "string", "description": "论文权威标题"},
        },
        "required": ["action", "title"],
    }

    def _format_entry(self, action: str, title: str) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return f"[{now}] {action}《{title}》"

    def execute(self, action: str, title: str) -> ToolResult:
        return ToolResult(text=self.append_line(action=action, title=title))
