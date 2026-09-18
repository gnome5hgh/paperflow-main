# paperflow/core/skill_install.py
"""skill 安装管理 —— CLI 准入通道（取源 → 校验 → 展示确认 → 落盘 → manifest）。

信任模型（spec §6.3）：CLI 安装 = 外部来源，必须过准入；手动拷目录 = 本地操作者，
与 <workspace>/agents/ 同级信任，扫描可见、list 标「未登记」，不做额外拦截。

ClawHub 供应链教训的对应防线：
- 含 tools.py 的 skill 强制过目：-y / 非交互下一律拒绝，除非显式 --allow-code（fail-closed）
- zip/tar 设大小与文件数上限（防 22MB 填充炸弹撑爆扫描管道）
- manifest 记录来源/版本/内容 hash/安装时间，为二期 update 与完整性校验留钩子
"""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from paperflow.core.frontmatter import parse_frontmatter

#: manifest 文件名（置于 skills/ 内，与被治理对象同处）
MANIFEST_NAME = ".manifest.json"

#: 压缩包安全上限：原始字节 / 解压后总字节 / 文件数（防填充炸弹与 zip 炸弹）
_ARCHIVE_MAX_BYTES = 50 * 1024 * 1024
_ARCHIVE_MAX_ENTRIES = 2000

#: skill 发现深度：源根目录本身或其一级子目录（兼容单仓多 skill）


def _skills_root(workspace: Path) -> Path:
    return workspace / "skills"


def manifest_path(workspace: Path) -> Path:
    return _skills_root(workspace) / MANIFEST_NAME


def load_manifest(workspace: Path) -> dict:
    """:returns: manifest 字典；文件缺失视为空（未登记任何安装）。"""
    p = manifest_path(workspace)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_manifest(workspace: Path, manifest: dict) -> None:
    """写 manifest（临时文件 + os.replace 原子替换——manifest 是治理记录，
    半截写入会让后续卸载/完整性校验读到损坏 JSON）。"""
    p = manifest_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _now_iso() -> str:
    """安装时间戳（ISO，秒级）——manifest 审计要素，二期 update 的基线。"""
    return datetime.now().isoformat(timespec="seconds")


def _hash_dir(path: Path) -> str:
    """目录内容 sha256（相对路径 + 文件字节，排序拼接），用于完整性校验。"""
    digest = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(f.relative_to(path)).encode("utf-8"))
        digest.update(f.read_bytes())
    return "sha256:" + digest.hexdigest()


def _is_git_source(source: str) -> bool:
    if source.startswith(("http://", "https://", "git@", "file://")) or source.endswith(".git"):
        return True
    # GitHub owner/repo 简写
    parts = source.split("/")
    return len(parts) == 2 and all(parts) and "://" not in source


def fetch_source(source: str, target: Path) -> Path:
    """把安装源落到 target 目录（本地目录直接复制，git 克隆，压缩包解压）。

    :param source: 本地目录 | git URL（含 owner/repo 简写、file://）| zip/tar 路径
    :param target: 空目录（调用方用 tempfile 保证）
    :returns: 含一个或多个 skill 目录的根路径
    :raises ValueError: 源不存在 / 压缩包超限 / git 克隆失败
    """
    p = Path(source)
    if p.is_dir():
        # 保留源目录名——skill 校验要求 frontmatter name == 目录名，落到固定名
        # （如 "src"）会让单 skill 目录源必然失配；无名目录（"." / "/"）回退 "src"。
        dest = target / (p.name or "src")
        shutil.copytree(p, dest, dirs_exist_ok=True)
        return dest
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
                tf.extractall(target, filter="data")
        return target
    if _is_git_source(source):
        url = source
        if len(source.split("/")) == 2 and "://" not in source and not source.startswith("git@"):
            url = f"https://github.com/{source}.git"
        subprocess.run(["git", "clone", "--depth", "1", url, str(target / "repo")],
                       check=True, capture_output=True)
        return target / "repo"
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

    :raises ValueError: 缺 name / name 与目录名不一致 / 缺 description（fail-fast）
    """
    meta, body = parse_frontmatter((path / "SKILL.md").read_text(encoding="utf-8"))
    name = meta.get("name")
    if not name or name != path.name:
        raise ValueError(f"skill 目录 '{path.name}' 的 name 缺失或与目录名不一致")
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


def install_skill(source: str, workspace: Path, *, assume_yes: bool = False,
                  allow_code: bool = False, confirm=None, print_fn=print) -> int:
    """准入安装一个来源中的全部合法 skill。

    :param confirm: 交互确认回调 confirm(展示文本) -> bool；缺省用 input()
    :returns: 0 全部成功；1 被拒绝/校验失败（已存在的 skill 视为失败，先 uninstall）
    """
    confirm = confirm or _default_confirm
    try:
        with tempfile.TemporaryDirectory(prefix="paperflow-skill-") as tmp:
            root = fetch_source(source, Path(tmp))
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
                    f"安装 {len(described)} 个 skill（{names}）到 {workspace}/skills/ ？"):
                print_fn("已取消")
                return 1
            # ---- 落盘 + manifest ----
            # 目标冲突先全量预检再落盘：多 skill 包中途撞名时不留「已拷贝但未登记」的半程状态
            for s in described:
                if (_skills_root(workspace) / s["name"]).exists():
                    print_fn(f"拒绝安装 '{s['name']}': 目标已存在（先 uninstall）")
                    return 1
            manifest = load_manifest(workspace)
            installed: list[Path] = []
            try:
                for s in described:
                    dest = _skills_root(workspace) / s["name"]
                    # 先登记再拷贝：copytree 建目录后中途失败不清理自建目录——若成功
                    # 后才登记，半成品目录会漏出回滚名单并永久阻塞同名重装（预检
                    # 「目标已存在」命中）
                    installed.append(dest)
                    shutil.copytree(s["path"], dest)
                    manifest[s["name"]] = {
                        "source": source,
                        "version": s["metadata"].get("version"),
                        "has_code": s["has_code"],
                        "hash": _hash_dir(dest),
                        "installed_at": _now_iso(),
                    }
                    print_fn(f"已安装: {s['name']} → {dest}")
                # manifest 写入同受回滚保护：写失败也回滚已拷目录，不留「已拷贝但无 manifest」
                _save_manifest(workspace, manifest)
            except BaseException:
                # 半程回滚：拷贝/manifest 写入中途失败（磁盘/权限等）不留
                # 「已落盘但未登记」目录，安装对 manifest 与 skills/ 同时保持全有或全无。
                for d in installed:
                    shutil.rmtree(d, ignore_errors=True)
                raise
            return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print_fn(f"安装失败: {e}")
        return 1


def uninstall_skill(name: str, workspace: Path, *, print_fn=print) -> int:
    """卸载 manifest 登记的 skill（内置/未登记不可卸载）。"""
    manifest = load_manifest(workspace)
    if name not in manifest:
        print_fn(f"无法卸载 '{name}': 未在 manifest 中登记（内置或手动拷贝的 skill 不可卸载）")
        return 1
    dest = _skills_root(workspace) / name
    if dest.exists():
        shutil.rmtree(dest)
    del manifest[name]
    _save_manifest(workspace, manifest)
    print_fn(f"已卸载: {name}")
    return 0


def list_skills_command(builtin_dir: str | None, workspace: Path, *, print_fn=print) -> int:
    """列出内置 + workspace skill，标注来源（内置/已装/未登记）、版本、是否含代码。"""
    from paperflow.core.skill_registry import SkillRegistry

    reg = SkillRegistry(builtin_dir=builtin_dir,
                        workspace_dir=str(_skills_root(workspace)))
    manifest = load_manifest(workspace)
    names = reg.list_skills()
    if not names:
        print_fn("（无 skill）")
        return 0
    for name in names:
        skill = reg.get_skill(name)
        if name in manifest:
            origin = "已装"
        elif builtin_dir and skill.path and Path(builtin_dir) in skill.path.parents:
            origin = "内置"
        else:
            origin = "未登记"
        print_fn(f"- {name}  [{origin}]  version={skill.metadata.get('version', '—')}"
                 f"  code={'yes' if skill.has_code else 'no'}")
    return 0
