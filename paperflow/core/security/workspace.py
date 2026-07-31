# paperflow/core/security/workspace.py
"""
WorkspacePolicyMiddleware —— 工作区路径边界检查中间件。

从 Tool.parameters 的 JSON Schema 中提取 ``format="path"`` 的参数，
将调用方传入的路径解析为绝对路径，并检查其是否落在工具的
``allowed_paths`` 白名单（相对于工作区解析）内；越界即抛
``SecurityBlocked``（violations 规则名为 ``workspace_boundary``）。

设计要点：
- ``resolve_path``：相对路径基于 workspace 拼接后统一 ``resolve()``，
  消除 ``..`` 与符号链接，绝对路径原样保留；
- ``check_path``：通过 ``Path.relative_to`` 判断前缀归属，天然阻断
  目录穿越（如 ``allowed/../../etc/passwd`` resolve 后跳出根目录）；
- 空 ``allowed_paths`` 视为拒绝所有路径（权限最小化，ADR 0003）；
- 无 ``format="path"`` 参数的工具直接放行，不引入额外开销。
"""

from pathlib import Path

from paperflow.core.security import SecurityMiddleware, ToolContext, SecurityBlocked


class WorkspacePolicy:
    @staticmethod
    def resolve_path(path: str, workspace: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(workspace) / candidate
        return candidate.resolve()

    @staticmethod
    def check_path(path: str | Path, allowed_roots: list[str]) -> bool:
        if not allowed_roots:
            return False
        resolved = Path(path).resolve()
        for root in allowed_roots:
            try:
                resolved.relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False


class WorkspacePolicyMiddleware(SecurityMiddleware):
    def __init__(self, workspace: str):
        self.workspace = workspace

    def _path_param_names(self, parameters: dict) -> set[str]:
        props = parameters.get("properties", {})
        return {k for k, v in props.items() if v.get("format") == "path"}

    async def before(self, ctx: ToolContext) -> None:
        if ctx.tool is None:
            return
        path_names = self._path_param_names(ctx.tool.parameters)
        if not path_names:
            return

        allowed = [
            str(WorkspacePolicy.resolve_path(r, self.workspace))
            for r in ctx.tool.allowed_paths
        ]

        violations = []
        for name in path_names:
            path = ctx.args.get(name)
            if not isinstance(path, str):
                continue
            resolved = WorkspacePolicy.resolve_path(path, self.workspace)
            if not WorkspacePolicy.check_path(str(resolved), allowed):
                violations.append({
                    "rule": "workspace_boundary",
                    "param": name,
                    "path": path,
                    "allowed": ctx.tool.allowed_paths,
                })

        if violations:
            raise SecurityBlocked(
                reason=f"路径越界: {', '.join(v['path'] for v in violations)}",
                violations=violations,
            )
