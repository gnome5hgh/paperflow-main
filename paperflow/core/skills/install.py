# paperflow/core/skills/install.py
"""skill 安装管理 —— CLI 准入通道（取源 → 校验 → 展示确认 → 落盘 → lock）。

信任模型（spec §6.3）：CLI 安装 = 外部来源，必须过准入；手动拷目录 = 本地操作者，
与 .paperflow/skills/ 内 git 提交的 skill 同级信任，扫描可见、list 标「未登记」，
不做额外拦截。

来源记录对齐业界惯例（Vercel skills-lock.json / Claude Code installed_plugins.json /
OpenClaw .clawhub/lock.json）：集中 lock 文件放在 skills 目录**之外**
（<pf_dir>/skills.lock.json），skill 目录本身保持与上游字节一致——完整性校验直接
重算目录哈希对照 lock，不必排除自家元数据文件，agent 也不会把安装元数据当
skill 上下文读入。

ClawHub 供应链教训的对应防线：
- 含 tools.py 的 skill 强制过目：-y / 非交互下一律拒绝，除非显式 --allow-code（fail-closed）
- zip/tar 设大小与文件数上限（防 22MB 填充炸弹撑爆扫描管道）
- lock 记录来源 pin（git 记 ref+精确 commit sha）/has_code/installed_at，为 update 留钩子；
  完整性锚点是 sha pin 而非内容哈希（对齐 Claude Code，无本地 verify）
"""

import inspect
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from paperflow.core.frontmatter import parse_frontmatter

#: skill name 字符集（agentskills.io 规范）：小写字母/数字/连字符，不以连字符
#: 开头结尾，无连续连字符。只做形态校验，不要求与来源目录名一致（见 describe_skill）。
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: lock 文件名（置于 <pf_dir>/ 根，在 skills/ 之外——集中记录惯例，见模块 docstring）
LOCK_NAME = "skills.lock.json"

#: lock 结构版本（schema version 惯例，对齐 Vercel skills-lock.json；结构变更时递增）
LOCK_VERSION = 1

#: 压缩包安全上限：原始字节 / 解压后总字节 / 文件数（防填充炸弹与 zip 炸弹）
_ARCHIVE_MAX_BYTES = 50 * 1024 * 1024
_ARCHIVE_MAX_ENTRIES = 2000

#: tarfile extractall 的 ``filter`` 形参能力探测（模块级算一次）：Python 3.11.0–3.11.3
#: 尚无该参数（3.11.4+ 引入、3.12+ 默认 "data"），传不存在的 kwarg 会 TypeError。
#: 不支持时保留原 extractall（与旧版行为一致；路径穿越防护由 3.12+ 默认 filter 补齐）。
_TAR_FILTER_SUPPORTED = "filter" in inspect.signature(
    tarfile.TarFile.extractall).parameters

#: skill 发现深度：源根目录本身或其一级子目录（兼容单仓多 skill）


def skills_root(pf_dir: Path) -> Path:
    """skill 落盘/扫描根：<pf_dir>/skills/。"""
    return pf_dir / "skills"


def lock_path(pf_dir: Path) -> Path:
    """集中 lock 文件：<pf_dir>/skills.lock.json（在 skills/ 之外）。"""
    return pf_dir / LOCK_NAME


def load_lock(pf_dir: Path) -> dict:
    """读 lock 的 skills 映射；文件缺失视为空（未登记任何安装）。

    :raises ValueError: schema version 与 LOCK_VERSION 不符（新版结构不得被旧代码
                        静默读写，对齐 Claude Code installed_plugins.json 版本策略）
    """
    p = lock_path(pf_dir)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("version") != LOCK_VERSION:
        raise ValueError(
            f"skills.lock.json schema 版本不符：期望 {LOCK_VERSION}，"
            f"得到 {data.get('version')!r}——请升级 paperflow 后重试")
    return data.get("skills", {})


def _save_lock(pf_dir: Path, skills: dict) -> None:
    """写 lock（临时文件 + os.replace 原子替换——lock 是治理记录，
    半截写入会让后续卸载/完整性校验读到损坏 JSON）。"""
    p = lock_path(pf_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    payload = {"version": LOCK_VERSION,
               # 按 skill 名排序：diff/合并稳定（Vercel skills-lock 同款约定）
               "skills": dict(sorted(skills.items()))}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _now_iso() -> str:
    """安装时间戳（ISO，秒级）——lock 审计要素，update 的 lastUpdated 基线。"""
    return datetime.now().isoformat(timespec="seconds")


def _is_git_source(source: str) -> bool:
    if source.startswith(("http://", "https://", "git@", "file://")) or source.endswith(".git"):
        return True
    # GitHub owner/repo 简写
    parts = source.split("/")
    return len(parts) == 2 and all(parts) and "://" not in source


def fetch_source(source: str, target: Path, *, ref: str | None = None) -> tuple[Path, dict]:
    """把安装源落到 target 目录（本地目录直接复制，git 克隆，压缩包解压）。

    :param source: 本地目录 | git URL（含 owner/repo 简写、file://）| zip/tar 路径
    :param target: 空目录（调用方用 tempfile 保证）
    :param ref: git 来源的分支/标签（仅 `git clone --branch` 支持的形态；None=默认 HEAD）
    :returns: (含一个或多个 skill 目录的根路径, 来源元数据)——git 记
              ``{"type","url","ref","sha"}``（sha=克隆后 HEAD，pin 精确 commit，
              对齐 Claude Code marketplace 条目）；local/archive 记 ``{"type","path"}``
    :raises ValueError: 源不存在 / 压缩包超限 / git 克隆失败
    """
    p = Path(source)
    if p.is_dir():
        # 保留源目录名拷入临时目录：让单仓多 skill 的相对结构在发现阶段原样保留
        #（发现按「根目录本身或一级子目录」找 SKILL.md，与目录名无关）；无名目录
        #（"." / "/"）回退 "src"。安装落盘名由 frontmatter name 决定
        #（dest = skills/<name>），与来源目录名是否同名无关。
        dest = target / (p.name or "src")
        shutil.copytree(p, dest, dirs_exist_ok=True)
        return dest, {"type": "local", "path": str(p.resolve())}
    # Path.suffix 只取最后一段，".tar.gz" 会得 ".gz"——用多级后缀拼接识别压缩包
    suffixes = "".join(p.suffixes).lower()
    if p.is_file() and suffixes in (".zip", ".tar", ".tar.gz", ".tgz"):
        if p.stat().st_size > _ARCHIVE_MAX_BYTES:
            raise ValueError(f"压缩包超过大小上限 {_ARCHIVE_MAX_BYTES} 字节: {source}")
        if suffixes == ".zip":
            with zipfile.ZipFile(p) as zf:
                _check_zipbomb(zf)
                zf.extractall(target)
        else:
            with tarfile.open(p) as tf:
                members = tf.getmembers()
                if len(members) > _ARCHIVE_MAX_ENTRIES:
                    raise ValueError(f"压缩包超过文件数上限 {_ARCHIVE_MAX_ENTRIES}")
                if sum(m.size for m in members) > _ARCHIVE_MAX_BYTES:
                    raise ValueError(f"压缩包解压后超过大小上限 {_ARCHIVE_MAX_BYTES} 字节")
                if _TAR_FILTER_SUPPORTED:
                    tf.extractall(target, filter="data")
                else:
                    # Python 3.11.0–3.11.3 无 filter 形参：保留原 extractall
                    tf.extractall(target)
        return target, {"type": "archive", "path": str(p.resolve())}
    if _is_git_source(source):
        url = source
        if len(source.split("/")) == 2 and "://" not in source and not source.startswith("git@"):
            url = f"https://github.com/{source}.git"
        clone_args = ["git", "clone", "--depth", "1"]
        if ref:
            clone_args += ["--branch", ref]
        clone_args += [url, str(target / "repo")]
        subprocess.run(clone_args, check=True, capture_output=True)
        repo = target / "repo"
        # pin 精确 commit：完整性锚点是 sha 而非目录内容哈希（Claude Code 模型）
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True).stdout.strip()
        return repo, {"type": "git", "url": url, "ref": ref, "sha": sha}
    raise ValueError(f"无法识别的安装源: {source}")


def _check_zipbomb(zf: zipfile.ZipFile) -> None:
    if len(zf.namelist()) > _ARCHIVE_MAX_ENTRIES:
        raise ValueError(f"压缩包超过文件数上限 {_ARCHIVE_MAX_ENTRIES}")
    total = sum(i.file_size for i in zf.infolist())
    if total > _ARCHIVE_MAX_BYTES:
        raise ValueError(f"压缩包解压后超过大小上限 {_ARCHIVE_MAX_BYTES} 字节")


def discover_skill_dirs(root: Path) -> list[Path]:
    """发现含 SKILL.md 的 skill 目录：根目录本身或其一级子目录（深 ≤2，兼容单仓多 skill）。"""
    if (root / "SKILL.md").exists():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


def describe_skill(path: Path) -> dict:
    """解析 skill 目录的 frontmatter，返回展示与校验所需信息。

    name 只校验存在/非空 + agentskills.io 字符集（小写字母/数字/连字符，不以
    连字符开头结尾，无连续连字符），**不要求与来源目录名一致**——git 仓库即包
    （SKILL.md 在仓库根，克隆目录名是 "repo"）、GitHub Download ZIP（内层
    xxx-main/）、flat zip（SKILL.md 在压缩包根，解到随机 tempdir）、`install .`
    （Path('.').name == ''）等业界标准来源形态的目录名都不是 name。
    「name == 目录名」不变量由 SkillRegistry 注册侧强校验（安装落盘用
    frontmatter name 命名目标目录 dest = skills/<name>，装好的副本天然满足）。

    :raises ValueError: 缺 name / name 字符集非法 / 缺 description（fail-fast）
    """
    meta, body = parse_frontmatter((path / "SKILL.md").read_text(encoding="utf-8"))
    name = meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"skill 目录 '{path.name}' 的 name 缺失或非法（agentskills.io 规范："
            "小写字母/数字/连字符，不以连字符开头结尾，无连续连字符）")
    if not str(meta.get("description", "")).strip():
        raise ValueError(f"skill '{name}' 缺少必填字段 'description'")
    has_code = (path / "tools.py").exists()
    # 资源清单：SKILL.md 之外的相对文件路径（references/、assets/ 等），准入展示用
    resources = sorted(str(f.relative_to(path))
                       for f in path.rglob("*") if f.is_file() and f.name != "SKILL.md")
    return {"name": name, "description": meta["description"],
            "metadata": meta.get("metadata") or {}, "has_code": has_code, "path": path,
            "license": meta.get("license"), "resources": resources}


def _default_confirm(prompt: str) -> bool:
    """缺省交互确认；EOF/中断（管道等非交互输入）视为拒绝——fail-closed。"""
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def install_skill(source: str, pf_dir: Path, *, ref: str | None = None,
                  assume_yes: bool = False, allow_code: bool = False,
                  confirm=None, print_fn=print) -> int:
    """准入安装一个来源中的全部合法 skill。

    :param pf_dir: .paperflow 根目录（skill 落盘其 skills/ 子目录，lock 在其根）
    :param ref: git 来源的分支/标签（lock 记录之，update 按其重装）
    :param confirm: 交互确认回调 confirm(展示文本) -> bool；缺省用 input()
    :returns: 0 全部成功；1 被拒绝/校验失败（已存在的 skill 视为失败，先 uninstall）
    """
    confirm = confirm or _default_confirm
    try:
        with tempfile.TemporaryDirectory(prefix="paperflow-skill-") as tmp:
            root, source_meta = fetch_source(source, Path(tmp), ref=ref)
            candidates = discover_skill_dirs(root)
            if not candidates:
                print_fn(f"源中未发现 skill（需含 SKILL.md）: {source}")
                return 1
            described = [describe_skill(p) for p in candidates]
            # ---- 准入展示 ----
            for s in described:
                print_fn(f"- {s['name']}  version={s['metadata'].get('version', '—')}")
                print_fn(f"  描述: {s['description'][:120]}")
                print_fn(f"  代码: {'⚠️ 捆绑 tools.py（可执行 Python）' if s['has_code'] else '无（纯指令包）'}")
                if s["has_code"]:
                    # 工具名/risk 汇总需 import 未审计代码，本期明确不做（spec 专项设计）
                    print_fn("  工具清单：安装前不执行未审计代码，暂不枚举（见 spec）")
                print_fn(f"  license: {s['license'] or '—'}")
                print_fn(f"  资源清单: {', '.join(s['resources']) or '无'}")
            # ---- 准入裁决 ----
            if any(s["has_code"] for s in described) and not allow_code:
                # fail-closed：含代码强制过目，-y/非交互不豁免（与确认看门狗同哲学）
                print_fn("拒绝安装：skill 捆绑可执行代码（tools.py）。"
                         "请人工审读代码后用 --allow-code 显式放行。")
                return 1
            names = "、".join(s["name"] for s in described)
            if not assume_yes and not confirm(
                    f"安装 {len(described)} 个 skill（{names}）到 {pf_dir}/skills/ ？"):
                print_fn("已取消")
                return 1
            # ---- 落盘 + lock ----
            # 目标冲突先全量预检再落盘：多 skill 包中途撞名时不留「已拷贝但未登记」的半程状态
            for s in described:
                if (skills_root(pf_dir) / s["name"]).exists():
                    print_fn(f"拒绝安装 '{s['name']}': 目标已存在（先 uninstall）")
                    return 1
            lock = load_lock(pf_dir)
            installed: list[Path] = []
            try:
                for s in described:
                    dest = skills_root(pf_dir) / s["name"]
                    # 先登记再拷贝：copytree 建目录后中途失败不清理自建目录——若成功
                    # 后才登记，半成品目录会漏出回滚名单并永久阻塞同名重装（预检
                    # 「目标已存在」命中）
                    installed.append(dest)
                    shutil.copytree(s["path"], dest)
                    is_git = source_meta["type"] == "git"
                    lock[s["name"]] = {
                        "source": {k: source_meta[k]
                                   for k in ("type", "url" if is_git else "path")},
                        "ref": source_meta.get("ref"),
                        "sha": source_meta.get("sha"),
                        "has_code": s["has_code"],
                        "enabled": True,
                        "installed_at": _now_iso(),
                    }
                    print_fn(f"已安装: {s['name']} → {dest}")
                # lock 写入同受回滚保护：写失败也回滚已拷目录，不留「已拷贝但无 lock」
                _save_lock(pf_dir, lock)
            except BaseException:
                # 半程回滚：拷贝/lock 写入中途失败（磁盘/权限等）不留
                # 「已落盘但未登记」目录，安装对 lock 与 skills/ 同时保持全有或全无。
                for d in installed:
                    shutil.rmtree(d, ignore_errors=True)
                raise
            if allow_code and any(s["has_code"] for s in described):
                print_fn("提示：捆绑 tools.py 的安全元数据将在下次启动时校验；"
                         "若装坏了可运行 `/skill uninstall <name>` 恢复。")
            return 0
    except (ValueError, OSError, subprocess.CalledProcessError,
            zipfile.BadZipFile, tarfile.TarError) as e:
        # BadZipFile/TarError：损坏压缩包 → 友好 rc=1，不裸 traceback
        print_fn(f"安装失败: {e}")
        return 1


def uninstall_skill(name: str, pf_dir: Path, *, print_fn=print) -> int:
    """卸载 lock 登记的 skill（未登记的（git 提交或手动拷贝）不可卸载）。"""
    lock = load_lock(pf_dir)
    if name not in lock:
        print_fn(f"无法卸载 '{name}': 未在 lock 中登记（git 提交或手动拷贝的 skill 不可卸载）")
        return 1
    dest = skills_root(pf_dir) / name
    if dest.exists():
        shutil.rmtree(dest)
    del lock[name]
    _save_lock(pf_dir, lock)
    print_fn(f"已卸载: {name}")
    return 0


def update_skill(name: str, pf_dir: Path, *, allow_code: bool = False,
                 print_fn=print) -> int:
    """更新 lock 登记的 git 来源 skill 到源最新版本（对齐 Claude Code plugin update）。

    按记录的 url+ref 重克隆 → 源中须有同名 skill → 准入重走（新版本含
    tools.py 必须显式 --allow-code，旧版本装过不豁免）→ 原子换目录 → 刷 lock。
    :returns: 0 成功；1 拒绝/校验失败（lock schema 不符经 ValueError 上抛）
    """
    lock = load_lock(pf_dir)
    entry = lock.get(name)
    if entry is None:
        print_fn(f"无法更新 '{name}': 未在 lock 中登记（git 提交或手动拷贝的 skill 不可更新）")
        return 1
    source = entry.get("source") or {}
    if source.get("type") != "git":
        print_fn(f"无法更新 '{name}': 来源类型为 {source.get('type', '未知')}，"
                 "仅 git 来源可更新（local/archive 源不可复现）")
        return 1
    ref = entry.get("ref")
    old_sha = entry.get("sha")
    try:
        with tempfile.TemporaryDirectory(prefix="paperflow-skill-update-") as tmp:
            root, source_meta = fetch_source(source["url"], Path(tmp), ref=ref)
            described = {d["name"]: d for d in
                         (describe_skill(p) for p in discover_skill_dirs(root))}
            if name not in described:
                print_fn(f"更新失败: 源 {source['url']}（ref={ref or '默认'}）"
                         f"中未找到同名 skill '{name}'")
                return 1
            if described[name]["has_code"] and not allow_code:
                # fail-closed：与 install 同门——新版本含代码必须人工过目后显式放行
                print_fn("拒绝更新：新版本捆绑可执行代码（tools.py）。"
                         "请人工审读代码后用 --allow-code 显式放行。")
                return 1
            dest = skills_root(pf_dir) / name
            backup = Path(tmp) / "old-version"
            if dest.exists():
                shutil.move(str(dest), str(backup))
            try:
                shutil.copytree(described[name]["path"], dest)
                entry.update({
                    "ref": source_meta.get("ref"),
                    "sha": source_meta.get("sha"),
                    "has_code": described[name]["has_code"],
                    "installed_at": _now_iso(),
                })
                _save_lock(pf_dir, lock)
            except BaseException:
                # 换目录半程失败：还原旧目录，lock 未写保持旧值（全有或全无）
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                if backup.exists():
                    shutil.move(str(backup), str(dest))
                raise
            print_fn(f"已更新: {name}  sha {old_sha or '—'} → {entry.get('sha') or '—'}")
            return 0
    except (ValueError, OSError, subprocess.CalledProcessError,
            zipfile.BadZipFile, tarfile.TarError) as e:
        print_fn(f"更新失败: {e}")
        return 1


def enable_skill(name: str, pf_dir: Path, *, enabled: bool, print_fn=print) -> int:
    """切换 lock 登记 skill 的启用状态（对齐 Claude Code enabledPlugins；幂等）。

    停用 = installed 但扫描不可见（L1/L2/L3 与工具并入全线跳过）。未登记的
    （git 提交/手动拷贝）不可切换——其移除/管理方式是删目录，写无来源的 lock
    条目会污染 uninstall 语义。
    """
    lock = load_lock(pf_dir)
    entry = lock.get(name)
    if entry is None:
        print_fn(f"无法{'停用' if not enabled else '启用'} '{name}': 未在 lock 中登记"
                 "（git 提交或手动拷贝的 skill 用删目录方式管理）")
        return 1
    if entry.get("enabled", True) == enabled:
        print_fn(f"'{name}' 已是{'启用' if enabled else '停用'}状态")
        return 0
    entry["enabled"] = enabled
    _save_lock(pf_dir, lock)
    print_fn(f"已{'启用' if enabled else '停用'}: {name}")
    return 0


def list_skills_command(skills_dir: str | None, pf_dir: Path, *, print_fn=print) -> int:
    """列出 skills 目录下的 skill，标注来源（已装=lock 登记/已停用/未登记）、版本、是否含代码。"""
    from paperflow.core.skills.registry import SkillRegistry

    reg = SkillRegistry(skills_dir=skills_dir)
    lock = load_lock(pf_dir)
    names = reg.list_skills()
    if not names:
        print_fn("（无 skill）")
        return 0
    for name in names:
        skill = reg.get_skill(name)
        entry = lock.get(name)
        if entry is not None and not entry.get("enabled", True):
            origin = "已停用"
        elif entry is not None:
            origin = "已装"
        else:
            origin = "未登记"
        print_fn(f"- {name}  [{origin}]  version={skill.metadata.get('version', '—')}"
                 f"  code={'yes' if skill.has_code else 'no'}")
    return 0
