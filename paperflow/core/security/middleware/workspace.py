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

    Args:
        resolved: 已解析为绝对路径的 Path 对象（须已 resolve()）
        workspace: 工作区根目录的字符串路径

    Returns:
        bool: 如果路径命中黑名单返回 True，否则返回 False
    """
    # 统一 resolve 确保比较时路径规范化
    # .resolve()：返回该路径的绝对路径，同时解析所有符号链接并消除.. /.等相对路径组件
    resolved = Path(resolved).resolve() # 用户请求的文件路径
    ws = Path(workspace).resolve()      # 允许访问的工作空间根目录

    # ----- 第一段：工作区内的系统运行时数据目录（精确匹配） -----
    # 使用 is_relative_to 判断 resolved 是否在 ws/audit 或 ws/chroma 之下，
    # 注意：这要求 audit 目录直接位于工作区根下，不会误伤工作区内名为 audit 的普通笔记文件夹。
    if resolved.is_relative_to(ws / "audit"):
        return True
    if resolved.is_relative_to(ws / "chroma"):
        return True

    # ----- 第二段：版本控制/配置目录（任何路径位置） -----
    # 将路径的各个组成部分（如 ["home", "user", "project", ".git", "config"]）转为小写后，
    # 与目标目录名集合做交集。若存在 .git 或 .claude 目录，则拒绝。
    # 大小写不敏感是为了兼容 macOS（默认大小写不敏感文件系统），防止攻击者用 .GIT 绕过。
    if {p.lower() for p in resolved.parts} & {".git", ".claude"}:
        return True

    # ----- 第三段：密钥文件（任何位置） -----
    # 检查文件名（不含路径）是否属于敏感配置文件。
    # 同样使用 lower() 做大小写不敏感匹配。
    if resolved.name.lower() in {"config.yaml", ".env", ".env.local"}:
        return True

    return False


class WorkspacePolicy:
    """路径解析与归属判断的纯函数集合，供中间件与外部代码复用。"""

    @staticmethod
    def resolve_path(path: str, workspace: str) -> Path:
        """把路径解析为绝对路径：相对路径基于工作区拼接，绝对路径原样保留。

        Args:
            path: 用户输入的路径字符串（可能是相对或绝对）
            workspace: 工作区根目录路径

        Returns:
            Path: 解析后的绝对路径（已调用 .resolve() 规范化）
        """
        # 先展开用户目录（~），然后判断是否绝对路径
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            # 是相对路径：以工作区为基座拼接
            candidate = Path(workspace) / candidate
        # resolve() 会消除 .. 和 . 符号，并处理符号链接（如存在）
        return candidate.resolve()

    @staticmethod
    def check_path(path: str | Path, allowed_roots: list[str]) -> bool:
        """判断路径是否落在任一允许根目录之下；允许根为空时一律拒绝。

        通过 `Path.relative_to` 判断前缀归属，天然阻断目录穿越攻击
        （如 `allowed/../../etc/passwd` 经 resolve 后会跳出 allowed 根，
        此时 relative_to 会抛出 ValueError，判定为不合法）。

        Args:
            path: 待检查的路径（字符串或 Path）
            allowed_roots: 允许的根目录列表（字符串路径，相对或绝对均可）

        Returns:
            bool: 若路径落在任意一个允许根下则返回 True，否则 False
        """
        if not allowed_roots:
            # 没有白名单则拒绝所有路径
            return False

        resolved = Path(path).resolve()
        for root in allowed_roots:
            # 将每个允许根也解析为绝对路径，确保比较基准统一
            try:
                # 如果 resolved 在 root 之下，relative_to 返回相对部分；否则抛出 ValueError
                # .relative_to(...)	：返回当前路径相对于参数路径的相对路径
                resolved.relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False


class WorkspacePolicyMiddleware(SecurityMiddleware):
    """工作区路径边界中间件：在工具执行前校验路径类参数不越界。"""

    def __init__(self, workspace: str):
        """指定工作区根目录；所有路径参数都相对它做白名单归属判断。

        Args:
            workspace: 工作区根目录的路径字符串
        """
        self.workspace = workspace

    def _path_param_names(self, parameters: dict) -> set[str]:
        """收集工具参数声明中所有 format="path" 的参数名。

        Args:
            parameters: 工具的 parameters 字典（JSON Schema 格式）

        Returns:
            set[str]: 所有声明为 path 格式的参数名集合
        """
        props = parameters.get("properties", {})
        return {k for k, v in props.items() if v.get("format") == "path"}

    async def before(self, ctx: ToolContext) -> None:
        """检查路径类参数：相对路径、敏感路径、越界路径分别记录违规并拦截。

        检查顺序：
            1. 如果工具不存在（ctx.tool is None）则跳过（由审计中间件记录未知工具）
            2. 获取工具参数中所有 format="path" 的参数名；若无则直接放行
            3. 预计算工具的 allowed_paths 白名单（将每个允许根解析为绝对路径）
            4. 对每个路径参数：
                a. 若值不是字符串则跳过（可能是其他类型）
                b. 若路径不是绝对路径 → 直接判违规（相对路径拒绝），记录 workspace_boundary
                c. 调用 resolve_path 解析为绝对路径
                d. 调用 is_denied_path 检查敏感路径黑名单 → 命中则记录 denied_path
                e. 调用 check_path 检查是否在白名单内 → 不在则记录 workspace_boundary
            5. 若存在违规，优先以敏感路径违规为理由，抛 SecurityBlocked

        Args:
            ctx: 工具调用上下文，包含工具定义、参数等

        Raises:
            SecurityBlocked: 当任意路径参数违规时抛出，携带违规明细列表
        """
        # 如果工具未知（LLM 幻觉或注入），跳过检查（审计中间件会记录错误）
        if ctx.tool is None:
            return

        # 收集所有 format="path" 的参数名
        path_names = self._path_param_names(ctx.tool.parameters)
        if not path_names:
            return

        # 将工具的 allowed_paths 白名单全部解析为绝对路径（以便后续比较）
        allowed = [
            str(WorkspacePolicy.resolve_path(r, self.workspace))
            for r in ctx.tool.allowed_paths
        ]

        violations = []
        for name in path_names:
            path = ctx.args.get(name)
            if not isinstance(path, str):
                # 参数存在但不是字符串（例如整数或布尔值），无法作为路径处理，跳过
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

            # 解析为绝对路径（基于工作区拼接，或原样保留绝对路径）
            resolved = WorkspacePolicy.resolve_path(path, self.workspace)

            # 敏感路径黑名单：在白名单之前硬拦截（防白名单根目录错位放行审计/密钥）
            if is_denied_path(resolved, self.workspace):
                violations.append({
                    "rule": "denied_path",
                    "param": name,
                    "path": path,
                    "reason": "敏感路径受保护",
                })
                continue

            # 白名单归属检查（判断解析后的路径是否在 allowed 根之下）
            if not WorkspacePolicy.check_path(str(resolved), allowed):
                violations.append({
                    "rule": "workspace_boundary",
                    "param": name,
                    "path": path,
                    "allowed": ctx.tool.allowed_paths, # 原始白名单（未解析），便于调试
                })

        # 如果有违规，构造 SecurityBlocked 异常并抛出
        if violations:
            # 优先以敏感路径违规作为主原因（敏感路径通常比越界更重要）
            denied = [v for v in violations if v["rule"] == "denied_path"]
            if denied:
                reason = f"敏感路径受保护: {', '.join(v['path'] for v in denied)}"
            else:
                reason = f"路径越界: {', '.join(v['path'] for v in violations)}"
            raise SecurityBlocked(reason=reason, violations=violations)
