# paperflow/core/security/middleware/workspace.py
"""
工作区路径边界检查中间件。

从工具参数声明中提取 ``format="path"`` 的参数，把调用方传入的路径解析为
绝对路径，并检查其是否落在工具的 ``allowed_paths`` 白名单（相对工作区解析）
内；越界即抛 ``SecurityBlocked``（违规规则名为 ``workspace_boundary``）。
白名单检查之前还会先过敏感路径黑名单（``is_denied_path``）：审计目录、向量库
目录、.git 与密钥文件名命中即拒绝（违规规则名为 ``denied_path``），防止
白名单根目录错位放行敏感文件。

设计要点：
- **相对路径直接拒绝**：工作区为外部绝对路径，相对路径无法可靠映射到任何
  根，因此不再猜测性地解析，直接判越界并给出可行动报错
  （reason="路径必须是绝对路径"），引导调用方改用绝对路径；
- ``resolve_path`` 静态方法语义**保持不变**：相对路径基于工作区拼接后统一
  ``resolve()``，绝对路径原样保留（外部代码直接依赖该方法）；
- ``check_path`` 通过 ``Path.relative_to`` 判断前缀归属，天然阻断目录穿越
  （如 ``allowed/../../etc/passwd`` 解析后跳出根目录）；
- 空 ``allowed_paths`` 视为拒绝所有路径（最小权限原则）；
- 没有 ``format="path"`` 参数的工具直接放行，不引入额外开销。
"""

import os
from pathlib import Path

from paperflow.core.security.base import SecurityMiddleware, ToolContext, SecurityBlocked


def is_denied_path(resolved: Path, workspace: str) -> bool:
    """敏感路径黑名单：白名单之前的硬拦截——命中即拒绝，无视白名单。

    分三段：
    ① 系统运行时数据：workspace/audit（审计日志防篡改）、workspace/chroma
       （向量库防绕过/防写坏）——精确绝对路径，工作区里同名文件夹（如笔记
       "audit"）不误伤。约定审计目录 = workspace/audit；若将来改为自定义
       目录，此派生需同步。
    ② 仓库内部段（任何位置）：.git / .claude（settings 可能含 API key）。
    ③ 密钥文件名（任何位置）：config.yaml / .env / .env.local。

    防配置错位：当工作区根与系统目录重叠时，白名单按相对前缀判断会放行审计
    日志——黑名单在此兜底。
    """
    resolved = Path(resolved).resolve()
    ws = Path(workspace).resolve()
    if resolved.is_relative_to(ws / "audit"):
        return True
    if resolved.is_relative_to(ws / "chroma"):
        return True
    # ②③ 段按大小写不敏感匹配：macOS 默认大小写不敏感的文件系统上，
    # ws/.ENV、ws/Config.yaml、ws/.GIT/config 与 .env/config.yaml/.git 是
    # 同一文件——大小写敏感比对会绕过密钥/审计防护。
    if {p.lower() for p in resolved.parts} & {".git", ".claude"}:
        return True
    if resolved.name.lower() in {"config.yaml", ".env", ".env.local"}:
        return True
    return False


class WorkspacePolicy:
    """路径解析与归属判断的纯函数集合，供中间件与外部代码复用。"""

    @staticmethod
    def resolve_path(path: str, workspace: str) -> Path:
        """把路径解析为绝对路径：相对路径基于工作区拼接，绝对路径原样保留。"""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(workspace) / candidate
        return candidate.resolve()

    @staticmethod
    def check_path(path: str | Path, allowed_roots: list[str]) -> bool:
        """判断路径是否落在任一允许根目录之下；允许根为空时一律拒绝。"""
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
    """工作区路径边界中间件：在工具执行前校验路径类参数不越界。"""

    def __init__(self, workspace: str):
        """指定工作区根目录；所有路径参数都相对它做白名单归属判断。"""
        self.workspace = workspace

    def _path_param_names(self, parameters: dict) -> set[str]:
        """收集工具参数声明中所有 format="path" 的参数名。"""
        props = parameters.get("properties", {})
        return {k for k, v in props.items() if v.get("format") == "path"}

    async def before(self, ctx: ToolContext) -> None:
        """检查路径类参数：相对路径、敏感路径、越界路径分别记录违规并拦截。"""
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
            # 相对路径直接拒绝：不解析到任何猜测的根。工作区是外部绝对路径，
            # 相对路径无法可靠映射；给出可行动的报错引导调用方改用绝对路径。
            if not os.path.isabs(path):
                violations.append({
                    "rule": "workspace_boundary",
                    "param": name,
                    "path": path,
                    "reason": "路径必须是绝对路径",
                })
                continue
            resolved = WorkspacePolicy.resolve_path(path, self.workspace)
            # 敏感路径黑名单：白名单之前的硬拦截（防白名单根目录错位放行审计/密钥）
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
