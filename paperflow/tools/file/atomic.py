"""原子文件写入：同目录临时文件 + os.replace，杜绝部分写入损坏。

写文件工具（write_file/edit_file）共用。POSIX 上 os.replace 原子——写入中途
崩溃/断电不会留下残缺目标文件（旧内容要么完整保留、要么被完整替换）。
"""
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """把 content 原子写入 path。

    临时文件与目标同目录（同文件系统，replace 不跨设备）；写入 fsync 落盘后再
    replace，替换前内容已持久；失败时清理临时文件不残留。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=".paperflow")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
