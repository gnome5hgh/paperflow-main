"""MemFS 测试：blocks ↔ markdown 投影双向同步 + 自动索引 + git 提交。"""
import tempfile
from pathlib import Path

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
from paperflow.core.memory.services.memfs import MemFS


def _setup():
    tmp = Path(tempfile.mkdtemp())
    bm = GitEnabledBlockManager(MemoryDB(tmp / "memory.db"), memfs_dir=tmp / "memory")
    memfs = bm.memfs
    return tmp, bm, memfs


def test_block_to_file_projection():
    tmp, bm, memfs = _setup()
    bm.create_block("persona", "身份")
    bm.create_block("feedback_testing", "规则")
    # persona 归 system/
    assert (tmp / "memory" / "system" / "persona.md").exists()
    # 非 system 块在顶层
    assert (tmp / "memory" / "feedback_testing.md").exists()
    content = (tmp / "memory" / "feedback_testing.md").read_text(encoding="utf-8")
    assert "规则" in content and "description" in content


def test_file_edit_to_block():
    tmp, bm, memfs = _setup()
    bm.create_block("feedback_testing", "规则")
    # 模拟人工编辑投影文件
    path = tmp / "memory" / "feedback_testing.md"
    path.write_text(path.read_text(encoding="utf-8").replace("规则", "新规则"),
                    encoding="utf-8")
    changed = memfs.detect_file_changes()
    assert len(changed) == 1 and "新规则" in changed[0].value


def test_detect_preserves_version():
    # detect_file_changes 回读的 Block 应保留 DB 中的真实 version（乐观锁计数）
    tmp, bm, memfs = _setup()
    bm.create_block("feedback_testing", "规则")
    bm.update_block_value("feedback_testing", "规则v2")
    path = tmp / "memory" / "feedback_testing.md"
    path.write_text(path.read_text(encoding="utf-8").replace("规则v2", "规则v3"),
                    encoding="utf-8")
    changed = memfs.detect_file_changes()
    assert len(changed) == 1 and changed[0].version == 2


def test_auto_index_generated():
    tmp, bm, memfs = _setup()
    bm.create_block("persona", "身份")
    bm.create_block("feedback_testing", "规则")
    index = tmp / "memory" / "memory_filesystem.md"
    assert index.exists()
    text = index.read_text(encoding="utf-8")
    assert "feedback_testing" in text
    # 每次块变更后重新生成：改后索引含新块
    bm.create_block("project_kg", "项目")
    assert "project_kg" in index.read_text(encoding="utf-8")


def test_git_commit_on_update():
    tmp, bm, memfs = _setup()
    bm.create_block("persona", "身份")
    bm.update_block_value("persona", "身份v2")
    # git 有提交记录
    log = bm._git_log()
    assert len(log) >= 2     # create + update 各至少一次提交


def test_no_empty_commit():
    # 无块变更时再次 commit 不应产生新提交
    tmp, bm, memfs = _setup()
    bm.create_block("persona", "身份")
    n = len(bm._git_log())
    bm._commit("noop")
    assert len(bm._git_log()) == n
