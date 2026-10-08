"""原子文件写入：同目录临时文件 + os.replace，杜绝部分写入损坏。

所有落盘的写点共用（写/编辑笔记、下载 PDF、保存图表、建模板骨架）。POSIX 上
os.replace 原子——写入中途崩溃/断电不会留下残缺目标文件，**并发读者也只会看到
完整的旧内容或完整的新内容，不会读到半写文件**。后者是「读工具不参与写互斥」的
前提：保证读者不见半写靠的是替换的原子性，而不是写互斥。
"""
import os
import tempfile
from pathlib import Path


def _atomic_replace(path: Path, payload, mode: str) -> None:
    """原子替换的共用实现：同目录临时文件 → fsync → os.replace。

    Args:
        path: Path，目标文件路径（父目录自动创建）
        payload: str | bytes，待写入的完整内容
        mode: str，打开临时文件的模式（"w" 走 UTF-8 文本，"wb" 为二进制）
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=".paperflow")
    try:
        # mkstemp 默认 0600，与常见 0644 习惯不一致；统一按 0644 落盘，
        # PAPERFLOW_FILE_MODE 可覆盖（如 0600）。
        try:
            os.chmod(tmp, int(os.environ.get("PAPERFLOW_FILE_MODE", "644"), 8))
        except (OSError, ValueError):
            pass  # 权限设置失败不阻断写入（如非 owner 文件系统）
        handle = os.fdopen(fd, "w", encoding="utf-8") if mode == "w" else os.fdopen(fd, "wb")
        with handle as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write(path: Path, content: str) -> None:
    """把 content 原子写入 path（文本，UTF-8）。

    Args:
        path: Path，目标文件路径（父目录自动创建）
        content: str，待写入的完整文本
    """
    _atomic_replace(path, content, "w")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """把二进制 content 原子写入 path。

    PDF / 图片这类字节内容不能走文本模式（会被按 UTF-8 撕裂），故单列一个入口。

    Args:
        path: Path，目标文件路径（父目录自动创建）
        content: bytes，待写入的完整字节内容
    """
    _atomic_replace(path, content, "wb")
