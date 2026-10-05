# paperflow/core/security/middleware/workspace.py
"""
工作区路径边界检查中间件。

从工具参数声明中提取 ``format="path"`` 的参数，做两道检查：
① 相对路径直接拒绝（``workspace_boundary``）——工作区为外部绝对路径，
   相对路径无法可靠映射，直接判越界并给出可行动报错；
② 敏感路径黑名单（``denied_path``）——审计目录、依赖服务数据卷、.git/.claude/.zcode、
   密钥与凭证文件、shell 配置、系统目录前缀，命中即拒绝。

白名单机制已退役（spec 2026-09-30）：path 工具统一「任意绝对路径 + 黑名单」，
产物落盘类参数的默认目录由 factory 装配时按 agent 注入，不在此层强制。
"""

import os
from pathlib import Path

from paperflow.core.security.base import SecurityMiddleware, ToolContext, SecurityBlocked


def _is_relative_to_ci(resolved: Path, prefix: Path) -> bool:
    """大小写不敏感的前缀归属判断：双侧路径部件小写后按部件边界比较。

    macOS 默认大小写不敏感文件系统（APFS）上 ``resolve()`` 不改写路径大小写
    （已实测：/USR/local/x -> /USR/local/x）——/USR 与 /usr、~/.SSH 与 ~/.ssh
    是同一目录，若前缀比较区分大小写即可绕过黑名单。与第二段「parts 小写」
    同一防绕过思路。按部件比较保证前缀语义精确：/usr 匹配 /usr/local，
    但不匹配 /usrlocal（裸字符串 startswith 会误伤/漏判）。

    Args:
        resolved: 已解析为绝对路径的 Path 对象（须已 resolve()）
        prefix: 前缀目录（Path 对象）

    Returns:
        bool: resolved 落在 prefix 之下（或相等）返回 True，否则 False
    """
    left = [p.lower() for p in resolved.parts]
    right = [p.lower() for p in prefix.parts]
    return len(left) >= len(right) and left[:len(right)] == right


def is_denied_path(resolved: Path, workspace: str) -> bool:
    """敏感路径黑名单：硬拦截——命中即拒绝。

    分六段：
    ① 系统运行时数据：workspace/security（审计日志防篡改）、workspace/infra
       （Milvus/GROBID 依赖服务数据卷防绕过/防写坏）——按工作区根下的模块前缀
       精确匹配，工作区里同名文件夹（如笔记 "security"）不误伤。约定审计目录
       = workspace/security/audit、服务卷 = workspace/infra/*；若将来改为
       自定义目录，此派生需同步。
    ② 仓库内部段（任何位置）：.git / .claude / .zcode（settings 可能含
       API key）。
    ③ 密钥文件名（任何位置）：config.yaml / .env / .env.local。
    ④ 凭证与秘密：home 目录下的 .ssh/.aws/.gnupg/.kube，以及任何位置的
       id_rsa/id_ed25519/id_ecdsa/.netrc 文件名与 .pem/.key 后缀。
    ⑤ shell 配置（任何位置）：.zshrc/.zprofile/.zshenv/.bashrc/
       .bash_profile/.profile，防持久化注入。
    ⑥ 系统目录前缀：/etc、/usr、/bin、/sbin、/System、/Library、/private/etc、
       /boot、/proc、/sys、/dev。

    前缀类比较（①④⑥）一律大小写不敏感（_is_relative_to_ci）：macOS APFS
    默认大小写不敏感，resolve() 不改写大小写，/USR、~/.SSH 不得绕过。

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

    # ----- 第一段：工作区内的系统运行时数据目录（前缀匹配，大小写不敏感） -----
    # 前缀判断 resolved 是否落在 ws/security 或 ws/infra 之下：审计目录与依赖
    # 服务数据卷按模块归位到这两个前缀（spec 2026-10-05-data-layout-by-module）。
    # 大小写不敏感：APFS 上 workspace/SECURITY 与 workspace/security 同目录（防绕过）。
    if _is_relative_to_ci(resolved, ws / "security"):
        return True
    if _is_relative_to_ci(resolved, ws / "infra"):
        return True

    # ----- 第二段：版本控制/配置目录（任何路径位置） -----
    # 将路径的各个组成部分（如 ["home", "user", "project", ".git", "config"]）转为小写后，
    # 与目标目录名集合做交集。若存在 .git / .claude / .zcode 目录，则拒绝。
    # 大小写不敏感是为了兼容 macOS（默认大小写不敏感文件系统），防止攻击者用 .GIT 绕过。
    if {p.lower() for p in resolved.parts} & {".git", ".claude", ".zcode"}:
        return True

    # ----- 第三段：密钥文件（任何位置） -----
    # 检查文件名（不含路径）是否属于敏感配置文件。
    # 同样使用 lower() 做大小写不敏感匹配。
    if resolved.name.lower() in {"config.yaml", ".env", ".env.local"}:
        return True

    # ----- 第四段：凭证与秘密（home 目录前缀 + 任何位置文件名/后缀） -----
    # home 前缀比较大小写不敏感（~/.SSH 与 ~/.ssh 在 APFS 上同目录）。
    home = Path.home()
    if any(_is_relative_to_ci(resolved, home / d)
           for d in (".ssh", ".aws", ".gnupg", ".kube")):
        return True
    if resolved.name.lower() in {"id_rsa", "id_ed25519", "id_ecdsa", ".netrc"}:
        return True
    if resolved.suffix.lower() in {".pem", ".key"}:
        return True

    # ----- 第五段：shell 配置（任何位置，防持久化注入） -----
    if resolved.name.lower() in {".zshrc", ".zprofile", ".zshenv", ".bashrc",
                                 ".bash_profile", ".profile"}:
        return True

    # ----- 第六段：系统目录前缀（resolve 已跟随符号链接，/etc→/private/etc 被覆盖） -----
    # 注意：用 /private/etc 而非裸 /private——macOS 上临时目录（/var/folders、/tmp）
    # resolve 后都落在 /private/ 之下，裸前缀会把所有临时路径判为敏感路径。
    # 前缀比较大小写不敏感：resolve() 不改写大小写，/USR、/library 不得绕过。
    if any(_is_relative_to_ci(resolved, Path(p)) for p in (
            "/etc", "/usr", "/bin", "/sbin", "/System", "/Library",
            "/private/etc", "/boot", "/proc", "/sys", "/dev")):
        return True

    return False


class WorkspacePolicy:
    """路径解析的纯函数集合，供中间件与外部代码复用。"""

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


class WorkspacePolicyMiddleware(SecurityMiddleware):
    """工作区路径边界中间件：工具执行前校验路径类参数为绝对路径且不触黑名单。"""

    def __init__(self, workspace: str):
        """指定工作区根目录；相对路径参数一律拒绝，绝对路径过黑名单。

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
        """检查路径类参数：相对路径拒绝、敏感路径黑名单硬拦。

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

            # 敏感路径黑名单：命中即拒绝
            if is_denied_path(resolved, self.workspace):
                violations.append({
                    "rule": "denied_path",
                    "param": name,
                    "path": path,
                    "reason": "敏感路径受保护",
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
