# tests/test_tools_factory.py
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.factory import make_tools


class RootTool(Tool):
    name = "root_tool"
    description = "declares semantic roots"
    parameters = {"type": "object", "properties": {}}
    allowed_roots = ["note", "pdf"]

    def execute(self) -> ToolResult:
        return ToolResult(text="ok")


def test_make_tools_resolves_roots(tmp_path):
    cfg = PaperFlowConfig(
        vault_note_dir=str(tmp_path / "note"),
        vault_pdf_dir=str(tmp_path / "pdf"),
        workspace="data",
    )
    tools = make_tools(cfg, [RootTool])
    assert len(tools) == 1
    tool = tools[0]
    assert tool.allowed_paths == [str(tmp_path / "note"), str(tmp_path / "pdf")]


def test_make_tools_memory_root(tmp_path):
    # 修正 brief 原测试：RootTool 仅声明 note/pdf，make_tools 只解析工具声明的 roots，
    # 故 memory 根必须由工具显式声明才会被注入（设计语义，见 tool.py allowed_roots 注释）。
    # 这里声明 memory 根的工具，验证 "memory → <workspace>/memory/" 的解析路径。
    class MemoryTool(Tool):
        name = "memory_tool"
        description = "declares memory root"
        parameters = {"type": "object", "properties": {}}
        allowed_roots = ["memory"]
        def execute(self): return ToolResult(text="ok")
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"))
    tools = make_tools(cfg, [MemoryTool])
    assert str(Path(tmp_path / "ws" / "memory")) in tools[0].allowed_paths


def test_make_tools_without_roots_empty():
    class NoRootTool(Tool):
        name = "nr"
        description = "no roots"
        parameters = {"type": "object", "properties": {}}
        def execute(self): return ToolResult(text="ok")
    cfg = PaperFlowConfig()
    tools = make_tools(cfg, [NoRootTool])
    assert tools[0].allowed_paths == []
