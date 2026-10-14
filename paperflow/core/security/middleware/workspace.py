# paperflow/core/security/middleware/workspace.py
"""
路径权威：读黑名单 + 写围栏。

从工具参数声明中提取 ``format="path"`` 的参数，做两道检查：
① 相对路径直接拒绝（``workspace_boundary``）——工作区为外部绝对路径，
   相对路径无法可靠映射，直接判越界并给出可行动报错；
② 敏感路径黑名单（``denied_path``）——审计目录、依赖服务数据卷、.git/.claude/.zcode、
   密钥与凭证文件、shell 配置、系统目录前缀，命中即拒绝。

检查通过后，参数**就地归一化为解析后的绝对路径**——写盘/读盘复用的正是这里检查过
的那一份结果，「检查的路径」与「用的路径」是同一个对象，中途替换软链的窗口因此不存在。

**本模块保证的四件事**（都由 ``Path.resolve()`` 这一个动作承载，不是四段各自的代码）：

1. **强制解析物理真实路径**——一切判断都做在 ``resolve()`` 的产物上，不在用户给的
   字符串上做。解析给出的是文件的真实身份，不是它的字面写法。
2. **拦截绝对路径穿越**——``..`` 在解析阶段被消解，之后的黑名单比较看到的已是
   消解后的绝对路径：``/note/../../etc/passwd`` 会先变成 ``/etc/passwd`` 再被拦下。
3. **递归解析祖先目录**——``resolve()`` 对**所有已存在的祖先**逐级跟随符号链接
   （叶子尚不存在也照跟），所以「祖先目录是个软链」这类逃逸在解析阶段就被拆开，
   不依赖调用方逐级校验。
4. **防软连接逃逸**——上面三条合起来的效果：``~/vault/link → /etc`` 时
   ``vault/link/passwd`` 解析成 ``/etc/passwd``，命中系统目录前缀被拒；
   软链不再是绕过路径。

已知边界：**硬链接**（``ln config.yaml /tmp/x`` 后写 ``/tmp/x``）在路径层面看不出来，
路径防护对它无效，只能靠「写类工具一律确认」兜底。

path 工具统一「任意绝对路径 + 黑名单」：
产物落盘类参数的默认目录由 factory 装配时按 agent 注入，不在此层强制。
"""

import os
from pathlib import Path

from paperflow.core.security.middleware.base import SecurityMiddleware, SecurityBlocked
from paperflow.core.security.domain import ToolContext
from paperflow.core.tool import is_writer


def _is_relative_to_ci(resolved: Path, prefix: Path) -> bool:
    """大小写不敏感的前缀归属判断：双侧路径部件小写后按部件边界比较。

    macOS 默认大小写不敏感文件系统（APFS）上 ``resolve()`` 不改写路径大小写
    （如 /USR/local/x -> /USR/local/x）——/USR 与 /usr、~/.SSH 与 ~/.ssh
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
       （Milvus 依赖服务数据卷防绕过/防写坏）——按工作区根下的模块前缀
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
    # 服务数据卷按模块归位到这两个前缀。
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
    """路径权威中间件：工具执行前校验路径类参数，并对写目标施加围栏。

    读与写两条语义不同：**读**是「任意绝对路径 + 敏感路径黑名单」（相对路径一律
    拒绝）；**写**额外要求落盘目标落在已声明的产物根之内，落在根外不拒绝、只登记
    升级因（由 PolicyEngineMiddleware 统一弹一次确认）。

    Attributes:
        workspace: str，工作区根目录路径（相对路径参数一律拒绝）
        write_roots: list[str]，写围栏的允许集合（已声明的产物根）
    """

    def __init__(self, workspace: str, write_roots: list[str] | None = None):
        """指定工作区根目录与已声明的产物根。

        Args:
            workspace: 工作区根目录的路径字符串
            write_roots: 写围栏的允许集合。None / 空 = 没有声明根，围栏无从判定
                         （对任何写目标都不登记升级因）；装配层应传入实际根。
        """
        self.workspace = workspace
        self.write_roots = [str(Path(r).resolve()) for r in (write_roots or [])]

    def _path_param_names(self, parameters: dict) -> set[str]:
        """收集工具参数声明中所有 format="path" 的参数名。

        Args:
            parameters: 工具的 parameters 字典（JSON Schema 格式）

        Returns:
            set[str]: 所有声明为 path 格式的参数名集合
        """
        props = parameters.get("properties", {})
        return {k for k, v in props.items() if v.get("format") == "path"}

    def _outside_write_roots(self, target: str) -> bool:
        """写目标是否落在已声明的产物根之外（按解析后的绝对路径比较）。

        Args:
            target: str，本次调用的有效目标路径（可含 ``~`` 或相对写法）

        Returns:
            bool: 落在全部声明根之外返回 True；无声明根时恒 False（围栏失效）。
        """
        if not self.write_roots:
            return False
        resolved = WorkspacePolicy.resolve_path(target, self.workspace)
        return not any(_is_relative_to_ci(resolved, Path(root))
                       for root in self.write_roots)

    async def before(self, ctx: ToolContext) -> None:
        """检查路径类参数（读）并施加写围栏（写）。

        读：相对路径拒绝、敏感路径黑名单硬拦。
        写：写类工具的落盘目标若在声明根之外，登记 ``path_fence`` 升级因——
        不抛异常、不拒绝，交给 PolicyEngine 统一弹确认。

        Args:
            ctx: 工具调用上下文，包含工具定义、参数等

        Raises:
            SecurityBlocked: 读路径参数违规时抛出，携带违规明细列表
        """
        # 如果工具未知（LLM 幻觉或注入），跳过检查（审计中间件会记录错误）
        if ctx.tool is None:
            return

        # ---- 读检查：format="path" 参数 ----
        violations = []
        normalized: dict[str, str] = {}
        for name in sorted(self._path_param_names(ctx.tool.parameters)):
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

            # 解析为物理真实路径（消解 ..、逐级跟随祖先软链）——本模块四条保证的载体
            resolved = WorkspacePolicy.resolve_path(path, self.workspace)

            # 敏感路径黑名单：命中即拒绝
            if is_denied_path(resolved, self.workspace):
                violations.append({
                    "rule": "denied_path",
                    "param": name,
                    "path": path,
                    "reason": "敏感路径受保护",
                })
                continue

            normalized[name] = str(resolved)

        if violations:
            # 优先以敏感路径违规作为主原因（敏感路径通常比越界更重要）
            denied = [v for v in violations if v["rule"] == "denied_path"]
            if denied:
                reason = f"敏感路径受保护: {', '.join(v['path'] for v in denied)}"
            else:
                reason = f"路径越界: {', '.join(v['path'] for v in violations)}"
            raise SecurityBlocked(reason=reason, violations=violations)

        # 就地归一化：把通过的路径参数换成解析后的绝对路径。工具随后写盘/读盘用的
        # 就是这一份——「检查的路径」与「用的路径」是同一个对象，中途替换软链的
        # 窗口因此不存在（检查与使用之间的 TOCTOU）。
        ctx.args.update(normalized)

        # ---- 写围栏：写类工具的落盘目标必须在声明根之内 ----
        # 走 effective_target_path（而不是 path 参数）——filename 便捷入口没有
        # path 参数，只有组合后的落盘路径能代表真实写入位置。
        if is_writer(ctx.tool):
            target = ctx.tool.effective_target_path(ctx.args)
            if isinstance(target, str) and self._outside_write_roots(target):
                ctx.escalations.append(("path_fence", target))
