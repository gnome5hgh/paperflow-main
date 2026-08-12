# tests/test_workspace.py
import pytest
from pathlib import Path
from paperflow.core.security import ToolContext, SecurityBlocked
from paperflow.core.security.middleware.workspace import (
    WorkspacePolicy, WorkspacePolicyMiddleware, is_denied_path,
)
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
        # 相对路径语义已改为拒绝；合法用例必须传绝对路径
        ctx = make_ctx(FileTool(), {"path": str(tmp_path / "paper" / "note" / "a.md")})
        await mw.before(ctx)  # 不应抛

    @pytest.mark.asyncio
    async def test_blocks_relative_path(self, tmp_path):
        mw = WorkspacePolicyMiddleware(workspace=str(tmp_path))
        ctx = make_ctx(FileTool(), {"path": "paper/note/a.md"})
        with pytest.raises(SecurityBlocked) as exc:
            await mw.before(ctx)
        assert exc.value.violations[0]["rule"] == "workspace_boundary"
        assert "必须是绝对路径" in exc.value.violations[0]["reason"]

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


class TestDeniedPath:
    def test_denies_workspace_audit(self, tmp_path):
        assert is_denied_path(tmp_path / "audit" / "a.jsonl", str(tmp_path)) is True
        assert is_denied_path(tmp_path / "audit", str(tmp_path)) is True

    def test_denies_workspace_chroma(self, tmp_path):
        assert is_denied_path(tmp_path / "chroma" / "x", str(tmp_path)) is True

    def test_denies_git_and_claude_segments(self, tmp_path):
        assert is_denied_path(tmp_path / ".git" / "config", str(tmp_path)) is True
        assert is_denied_path(tmp_path / ".claude" / "settings.json", str(tmp_path)) is True

    def test_denies_secret_filenames(self, tmp_path):
        assert is_denied_path(tmp_path / "config.yaml", str(tmp_path)) is True
        assert is_denied_path(tmp_path / ".env", str(tmp_path)) is True
        assert is_denied_path(tmp_path / ".env.local", str(tmp_path)) is True

    def test_denies_case_variants(self, tmp_path):
        """2026-08-07 final review：macOS 默认大小写不敏感 APFS 上，.ENV/Config.yaml/.GIT
        与 .env/config.yaml/.git 是同一文件——大小写敏感匹配是密钥/审计防护的真实绕过。
        is_denied_path 必须按大小写不敏感处理。"""
        assert is_denied_path(tmp_path / ".ENV", str(tmp_path)) is True
        assert is_denied_path(tmp_path / "Config.yaml", str(tmp_path)) is True
        assert is_denied_path(tmp_path / ".GIT" / "config", str(tmp_path)) is True

    def test_allows_intended_roots(self, tmp_path):
        # memory/templates 是允许根（记忆/模板功能）；vault 正常路径、同名 audit 文件夹不误伤
        assert is_denied_path(tmp_path / "memory" / "MEMORY.md", str(tmp_path)) is False
        assert is_denied_path(tmp_path / "templates" / "paper_note.md", str(tmp_path)) is False
        assert is_denied_path(tmp_path / "note" / "a.md", str(tmp_path)) is False
        # vault 里同名 "audit" 文件夹（不在 workspace/audit）不误伤
        assert is_denied_path(tmp_path / "vault" / "audit" / "notes.md", str(tmp_path)) is False


class TestMiddlewareDeniedPath:
    @pytest.mark.asyncio
    async def test_blocks_denied_even_if_allowed_root_overlaps(self, tmp_path):
        """白名单会放行（allowed root 含 audit 父目录），但 deny-list 硬拦——防配置错位。"""
        mw = WorkspacePolicyMiddleware(workspace=str(tmp_path))
        (tmp_path / "audit").mkdir(parents=True, exist_ok=True)
        tool = FileTool()
        tool.allowed_paths = [str(tmp_path)]   # 模拟错位：allowed root 覆盖整个 workspace
        ctx = make_ctx(tool, {"path": str(tmp_path / "audit" / "audit_x.jsonl")})
        with pytest.raises(SecurityBlocked) as exc:
            await mw.before(ctx)
        assert exc.value.violations[0]["rule"] == "denied_path"

    @pytest.mark.asyncio
    async def test_blocks_env_filename(self, tmp_path):
        mw = WorkspacePolicyMiddleware(workspace=str(tmp_path))
        tool = FileTool(); tool.allowed_paths = [str(tmp_path)]
        ctx = make_ctx(tool, {"path": str(tmp_path / ".env")})
        with pytest.raises(SecurityBlocked) as exc:
            await mw.before(ctx)
        assert exc.value.violations[0]["rule"] == "denied_path"

    @pytest.mark.asyncio
    async def test_allows_memory_root(self, tmp_path):
        mw = WorkspacePolicyMiddleware(workspace=str(tmp_path))
        tool = FileTool(); tool.allowed_paths = [str(tmp_path)]
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
        ctx = make_ctx(tool, {"path": str(tmp_path / "memory" / "MEMORY.md")})
        await mw.before(ctx)   # 不应抛（memory 是允许根）
