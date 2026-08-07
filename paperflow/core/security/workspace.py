# paperflow/core/security/workspace.py
"""
WorkspacePolicyMiddleware —— 工作区路径边界检查中间件。

从 Tool.parameters 的 JSON Schema 中提取 ``format="path"`` 的参数，
将调用方传入的路径解析为绝对路径，并检查其是否落在工具的
``allowed_paths`` 白名单（相对于工作区解析）内；越界即抛
``SecurityBlocked``（violations 规则名为 ``workspace_boundary``）。
2026-08-07 起在 resolve 之后、白名单检查之前插入敏感路径黑名单
（``is_denied_path``）：审计/chroma/.git/密钥文件名命中即拒绝，
violations 规则名为 ``denied_path``——防 allowed root 错位放行敏感文件。

设计要点：
- **相对路径直接拒绝**（Layer 2 起）：vault 为外部绝对路径，相对路径
  无法可靠映射到任何根，因此不再 resolve 到猜测的根，直接判越界并给出
  可行动报错（reason="路径必须是绝对路径"），引导 LLM 改用绝对路径；
- ``resolve_path`` 静态方法语义**保持不变**：相对路径基于 workspace 拼接后
  统一 ``resolve()``，绝对路径原样保留（Layer 1 测试直接依赖该静态方法）；
- ``check_path``：通过 ``Path.relative_to`` 判断前缀归属，天然阻断
  目录穿越（如 ``allowed/../../etc/passwd`` resolve 后跳出根目录）；
- 空 ``allowed_paths`` 视为拒绝所有路径（权限最小化，ADR 0003）；
- 无 ``format="path"`` 参数的工具直接放行，不引入额外开销。
"""

import os
from pathlib import Path

from paperflow.core.security import SecurityMiddleware, ToolContext, SecurityBlocked


def is_denied_path(resolved: Path, workspace: str) -> bool:
    """敏感路径黑名单（2026-08-07）：白名单之前的硬拦截——命中即拒绝，无视 allowed_paths。

    三段：
    ① 系统运行时数据：workspace/audit（审计日志防篡改）、workspace/chroma（向量库防绕过/
       防写坏）——精确绝对路径，vault 里同名文件夹（如笔记"audit"）不误伤。
       约定：审计目录 = workspace/audit（AuditMiddleware 默认 data/audit）；若将来配自定义
       audit_dir，此派生须同步。
    ② 仓库内部段（任何位置）：.git / .claude（settings 可能含 API key）。
    ③ 密钥文件名（任何位置）：config.yaml / .env / .env.local。

    防配置错位：vault 根与系统目录重叠时，白名单 relative_to(vault) 会放行审计日志——
    黑名单在此兜底。
    """
    resolved = Path(resolved).resolve()
    ws = Path(workspace).resolve()
    if resolved.is_relative_to(ws / "audit"):
        return True
    if resolved.is_relative_to(ws / "chroma"):
        return True
    if set(resolved.parts) & {".git", ".claude"}:
        return True
    if resolved.name in {"config.yaml", ".env", ".env.local"}:
        return True
    return False


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
            # 相对路径直接拒绝：不 resolve 到任何猜测的根（vault 为外部绝对路径，
            # 相对路径无法可靠映射；给出可行动的报错引导 LLM 改用绝对路径）
            if not os.path.isabs(path):
                violations.append({
                    "rule": "workspace_boundary",
                    "param": name,
                    "path": path,
                    "reason": "路径必须是绝对路径",
                })
                continue
            resolved = WorkspacePolicy.resolve_path(path, self.workspace)
            # 敏感路径黑名单：白名单之前的硬拦（防 allowed root 错位放行审计/密钥）
            if is_denied_path(resolved, self.workspace):
                violations.append({
                    "rule": "denied_path",
                    "param": name,
                    "path": path,
                    "reason": "敏感路径受保护",
                })
                continue
            if not WorkspacePolicy.check_path(str(resolved), allowed):
                violations.append({
                    "rule": "workspace_boundary",
                    "param": name,
                    "path": path,
                    "allowed": ctx.tool.allowed_paths,
                })

        if violations:
            denied = [v for v in violations if v["rule"] == "denied_path"]
            if denied:
                reason = f"敏感路径受保护: {', '.join(v['path'] for v in denied)}"
            else:
                reason = f"路径越界: {', '.join(v['path'] for v in violations)}"
            raise SecurityBlocked(reason=reason, violations=violations)
