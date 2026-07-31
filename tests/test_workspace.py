# tests/test_workspace.py
import pytest
from pathlib import Path
from paperflow.core.security import ToolContext, SecurityBlocked
from paperflow.core.security.workspace import WorkspacePolicy, WorkspacePolicyMiddleware
from paperflow.core.tool import Tool, ToolResult


class FileTool(Tool):
    name = "file_tool"
    description = "writes files"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path"},
            "content": {"type": "string", "format": "content"},
        },
        "required": ["path"],
    }
    allowed_paths = ["paper/note/"]

    def execute(self, path, content="") -> ToolResult:
        return ToolResult(text="ok")


class NoPathTool(Tool):
    name = "no_path"
    description = "no file access"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    }
    allowed_paths = []

    def execute(self, query="") -> ToolResult:
        return ToolResult(text="ok")


def make_ctx(tool, args):
    return ToolContext(
        trace_id="t", session_id="s", agent_type="test",
        tool=tool, tool_name=tool.name, args=args,
    )


class TestWorkspacePolicy:
    def test_resolve_relative_under_workspace(self, tmp_path):
        result = WorkspacePolicy.resolve_path("paper/note/a.md", str(tmp_path))
        assert result == (tmp_path / "paper/note/a.md").resolve()

    def test_resolve_absolute_stays(self, tmp_path):
        target = tmp_path / "x.md"
        result = WorkspacePolicy.resolve_path(str(target), str(tmp_path))
        assert result == target.resolve()

    def test_check_path_allows_within_root(self, tmp_path):
        root = str(tmp_path)
        assert WorkspacePolicy.check_path(str(tmp_path / "a.md"), [root]) is True

    def test_check_path_blocks_outside_root(self, tmp_path):
        root = str(tmp_path / "allowed")
        assert WorkspacePolicy.check_path(str(tmp_path / "other" / "a.md"), [root]) is False

    def test_check_path_empty_roots_blocks_all(self, tmp_path):
        assert WorkspacePolicy.check_path(str(tmp_path / "a.md"), []) is False

    def test_check_path_blocks_traversal(self, tmp_path):
        root = str(tmp_path / "allowed")
        escaped = str(tmp_path / "allowed" / ".." / ".." / "etc" / "passwd")
        assert WorkspacePolicy.check_path(escaped, [root]) is False


class TestWorkspacePolicyMiddleware:
    @pytest.mark.asyncio
    async def test_allows_legit_path(self, tmp_path):
        mw = WorkspacePolicyMiddleware(workspace=str(tmp_path))
        ctx = make_ctx(FileTool(), {"path": "paper/note/a.md"})
        await mw.before(ctx)  # 不应抛

    @pytest.mark.asyncio
    async def test_blocks_outside_path(self, tmp_path):
        mw = WorkspacePolicyMiddleware(workspace=str(tmp_path))
        ctx = make_ctx(FileTool(), {"path": "../outside.md"})
        with pytest.raises(SecurityBlocked) as exc:
            await mw.before(ctx)
        assert exc.value.violations[0]["rule"] == "workspace_boundary"

    @pytest.mark.asyncio
    async def test_blocks_when_no_allowed_paths(self, tmp_path):
        mw = WorkspacePolicyMiddleware(workspace=str(tmp_path))
        tool = NoPathTool()
        tool.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "format": "path"},
            },
        }
        ctx = make_ctx(tool, {"path": "paper/note/a.md"})
        with pytest.raises(SecurityBlocked):
            await mw.before(ctx)

    @pytest.mark.asyncio
    async def test_skips_when_no_path_params(self, tmp_path):
        mw = WorkspacePolicyMiddleware(workspace=str(tmp_path))
        ctx = make_ctx(NoPathTool(), {"query": "circRNA"})
        await mw.before(ctx)  # 不应抛
