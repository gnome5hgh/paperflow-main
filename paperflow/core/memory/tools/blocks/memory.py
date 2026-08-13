"""MemoryTool：统一记忆块管理（create / replace / delete / rename）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context
from paperflow.core.memory.tools.blocks._common import rewrite_block


class MemoryTool(Tool):
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
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            bm = ctx.block_manager
            # label 在 blocks 表无 UNIQUE 约束——create/rename 必须先查重，
            # 否则重复 label 会静默建出不可达的幽灵块（逻辑原样迁自 base.py memory）。
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
