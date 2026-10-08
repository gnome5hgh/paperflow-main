"""MemoryTool：统一记忆块管理（create / replace / delete / rename）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.memory.runtime_context import get_memory_context
from paperflow.tools.memory.blocks._common import rewrite_block


class MemoryTool(Tool):
    """统一记忆块管理工具（create/replace/delete/rename 四动作分发）。

    Attributes:
        name: str，工具名 "memory"
        description: str，工具描述（告诉模型何时用）
        parameters: dict，JSON Schema（action/label/value）
        risk_level: str，"medium"（写入类）
    """
    name = "memory"
    description = "统一记忆块管理（create/replace/delete/rename）"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "replace", "delete", "rename"]},
            "label": {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["action", "label"],
    }
    risk_level = "medium"

    def execute(self, action: str, label: str, value: str | None = None,
                **kwargs) -> ToolResult:
        """按 action 分发到块的增改删查；错误一律降级为文本返回（errors-as-data）。

        Args:
            action: str，动作：create | replace | delete | rename
            label: str，目标块标签
            value: str | None，块内容（create/replace 用）
            kwargs: dict，rename 时的新标签 new_label

        Returns:
            ToolResult；未装配记忆服务/标签冲突/只读/未知动作都降级为文本（errors-as-data）。
        """
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            bm = ctx.block_manager
            # label 在 blocks 表无 UNIQUE 约束——create/rename 必须先查重，
            # 否则重复 label 会静默建出「查得到却取不到」的不可达幽灵块。
            if action == "create":
                if bm.get_block_by_label(label) is not None:
                    return ToolResult(text=f"Error: label '{label}' already exists")
                bm.create_block(label, value or "")
                return ToolResult(text=f"Created block {label}")
            if action == "replace":
                return ToolResult(text=rewrite_block(bm, label, value or ""))
            if action == "delete":
                b = bm.get_block_by_label(label)
                if b is None:
                    return ToolResult(text=f"Error: no block with label {label}")
                if b.read_only:
                    return ToolResult(text="Error: block is read-only")
                bm.delete_block(b.id)
                return ToolResult(text=f"Deleted block {label}")
            if action == "rename":
                b = bm.get_block_by_label(label)
                if b is None:
                    return ToolResult(text=f"Error: no block with label {label}")
                new_label = kwargs.get("new_label", value or label)
                if new_label == label:
                    return ToolResult(text=f"Renamed block {label} -> {new_label}")
                if bm.get_block_by_label(new_label) is not None:
                    return ToolResult(text=f"Error: label '{new_label}' already exists")
                bm.create_block(new_label, b.value, limit=b.limit,
                                description=b.description, read_only=b.read_only)
                # rename 是重建（保护元数据迁移到新块），不是删除——走不检查 read_only
                # 的底层 _delete；直接 delete_block 会因 read_only 拒绝留下半完成的孤儿新块。
                bm._delete(b.id)
                return ToolResult(text=f"Renamed block {label} -> {new_label}")
            return ToolResult(text=f"Error: unknown action {action}")
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
