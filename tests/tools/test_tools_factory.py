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


from paperflow.tools import ReadFileTool, WriteFileTool, EditFileTool, FormatCheckTool


def test_make_tools_resolves_templates_and_scratch_roots(tmp_path):
    cfg = PaperFlowConfig(
        workspace=str(tmp_path / "ws"),
        vault_note_dir=str(tmp_path / "note"),
        vault_pdf_dir=str(tmp_path / "pdf"),
    )
    tools = make_tools(cfg, [ReadFileTool])
    allowed = tools[0].allowed_paths
    assert str(tmp_path / "ws" / "templates") in allowed
    assert str(tmp_path / "ws" / "tmp") in allowed


def test_format_check_allows_scratch_not_write(tmp_path):
    # vault_pdf_dir 显式给出，否则默认落真实用户 vault 路径，下方 pdf 断言退化为恒真
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"), vault_pdf_dir=str(tmp_path / "pdf"))
    tools = {t.name: t for t in make_tools(cfg, [ReadFileTool, WriteFileTool, EditFileTool, FormatCheckTool])}
    assert str(tmp_path / "ws" / "tmp") in tools["format_check"].allowed_paths
    # Paper 只读 + scratch 只读：Write/Edit 不含 pdf 也不含 scratch
    for name in ("write_file", "edit_file"):
        assert str(tmp_path / "ws" / "tmp") not in tools[name].allowed_paths
        assert str(tmp_path / "pdf") not in tools[name].allowed_paths


from paperflow.tools import ReadFileTool


def test_make_tools_injects_config_and_path_hints(tmp_path):
    cfg = PaperFlowConfig(
        workspace=str(tmp_path / "ws"),
        vault_note_dir=str(tmp_path / "note"),
        vault_pdf_dir=str(tmp_path / "pdf"),
    )
    tool = make_tools(cfg, [ReadFileTool])[0]
    assert tool._config is cfg                         # _config 注入
    assert f"note={str(tmp_path / 'note')}" in tool.description   # 路径提示
    assert f"templates={str(tmp_path / 'ws' / 'templates')}" in tool.description
    assert "scratch=" not in tool.description          # scratch 不透明
