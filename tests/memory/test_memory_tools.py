"""记忆工具测试：memory_replace 唯一子串 / memory_insert 行号 / read_only 拒绝 / archival 写入。"""
import tempfile
from pathlib import Path

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.passage_manager import PassageManager
from paperflow.core.memory.services.tool_manager import ToolManager
from paperflow.core.memory import constants


def _tools():
    tmp = tempfile.mkdtemp()
    db = MemoryDB(Path(tmp) / "memory.db")
    bm = BlockManager(db)
    pm = PassageManager(db)
    mm = MessageManager(db)
    tm = ToolManager(db)
    tm.bind(bm, pm, mm, agent_id="sess_1")
    tm.upsert_base_tools()
    return tm, bm, pm


def test_base_memory_tools_names():
    assert "memory_replace" in constants.BASE_MEMORY_TOOLS
    assert "memory_apply_patch" in constants.BASE_MEMORY_TOOLS
    assert constants.BASE_SLEEPTIME_TOOLS <= constants.BASE_MEMORY_TOOLS


def test_upsert_base_tools_registers_all():
    tm, _, _ = _tools()
    names = {t.name for t in tm.list_tools()}
    assert constants.BASE_MEMORY_TOOLS <= names
    assert "archival_memory_insert" in names
    assert "conversation_search" in names


def test_read_only_search_tools_risk_low():
    """只读检索工具（search）risk=low；记忆编辑（写）risk=medium。"""
    tm, _, _ = _tools()
    by_name = {t.name: t for t in tm.list_tools()}
    assert by_name["archival_memory_search"].risk_level == "low"
    assert by_name["conversation_search"].risk_level == "low"
    assert by_name["archival_memory_insert"].risk_level == "medium"
    assert by_name["memory_replace"].risk_level == "medium"


def test_memory_replace_unique_substring():
    tm, bm, _ = _tools()
    bm.create_block("feedback_testing", "规则一\n规则二")
    res = tm.execute_tool("memory_replace", {
        "label": "feedback_testing",
        "old_string": "规则一", "new_string": "新规则"}, "tc1")
    assert "新规则" in res.text
    assert "规则一" not in bm.get_block_by_label("feedback_testing").value


def test_memory_replace_multiple_match_errors():
    tm, bm, _ = _tools()
    bm.create_block("feedback_testing", "重复\n重复")
    res = tm.execute_tool("memory_replace", {
        "label": "feedback_testing", "old_string": "重复", "new_string": "x"}, "tc1")
    assert "error" in res.text.lower() or "multiple" in res.text.lower()


def test_memory_replace_read_only_rejected():
    tm, bm, _ = _tools()
    bm.create_block("persona", "身份", read_only=True)
    res = tm.execute_tool("memory_replace", {
        "label": "persona", "old_string": "身份", "new_string": "x"}, "tc1")
    assert "read" in res.text.lower()


def test_memory_insert_line():
    tm, bm, _ = _tools()
    bm.create_block("feedback_testing", "line1\nline2")
    tm.execute_tool("memory_insert", {"label": "feedback_testing",
                                      "new_string": "插播", "insert_line": 0}, "tc1")
    value = bm.get_block_by_label("feedback_testing").value
    assert value.splitlines()[0] == "插播"


def test_memory_apply_patch_removes_and_adds_lines():
    """评审 I-6：简化 unified diff 就地应用——- 行在对应位置删除、+ 行原位插入
    （而非「删全部 - 行再把 + 行追加末尾」）。"""
    tm, bm, _ = _tools()
    bm.create_block("feedback_testing", "line1\nline2\nline3")
    res = tm.execute_tool("memory_apply_patch", {
        "label": "feedback_testing", "patch": "-line2\n+line2b"}, "tc1")
    assert "Applied patch" in res.text
    value = bm.get_block_by_label("feedback_testing").value
    assert value == "line1\nline2b\nline3"   # 原位替换，不落到末尾


def test_memory_apply_patch_with_context_and_hunks():
    """带 @@ 头部 + ' ' 上下文锚定的真实 unified diff：跨 hunk 中间未改动行保留。"""
    tm, bm, _ = _tools()
    bm.create_block("feedback_testing", "line1\nline2\nline3\nline4")
    patch = "@@ -1,3 +1,3 @@\n line1\n-line2\n+line2b\n line3\n@@ -4,1 +4,1 @@\n-line4\n+line4b"
    res = tm.execute_tool("memory_apply_patch", {
        "label": "feedback_testing", "patch": patch}, "tc1")
    assert "Applied patch" in res.text
    value = bm.get_block_by_label("feedback_testing").value
    assert value == "line1\nline2b\nline3\nline4b"


def test_memory_apply_patch_missing_line_errors():
    """patch 引用的行在块里不存在 → 明确报错，块内容不变。"""
    tm, bm, _ = _tools()
    bm.create_block("feedback_testing", "line1\nline2")
    res = tm.execute_tool("memory_apply_patch", {
        "label": "feedback_testing", "patch": "-nope\n+x"}, "tc1")
    assert "error" in res.text.lower()
    assert bm.get_block_by_label("feedback_testing").value == "line1\nline2"


def test_memory_apply_patch_multi_block_rejected():
    tm, bm, _ = _tools()
    bm.create_block("feedback_testing", "v1")
    res = tm.execute_tool("memory_apply_patch", {
        "label": "feedback_testing",
        "patch": "*** Update Block: feedback_testing\n*** Add Block: x\n"}, "tc1")
    assert "not supported" in res.text   # 只支持单块模式
    assert bm.get_block_by_label("feedback_testing").value == "v1"


def test_memory_apply_patch_missing_block_errors():
    tm, bm, _ = _tools()
    res = tm.execute_tool("memory_apply_patch", {
        "label": "nope", "patch": "-x"}, "tc1")
    assert "no block" in res.text


def test_archival_insert_and_search():
    tm, bm, pm = _tools()
    tm.execute_tool("archival_memory_insert",
                    {"content": "GraphCL 结论", "tags": ["reading"]}, "tc1")
    assert pm.agent_passage_size("sess_1") == 1
    res = tm.execute_tool("archival_memory_search", {"query": "", "tags": ["reading"]}, "tc2")
    assert "GraphCL" in res.text


def test_memory_delete_read_only_rejected():
    """评审 I-5：memory delete 不能删 read_only 保护块（persona 等）。"""
    tm, bm, _ = _tools()
    bm.create_block("persona", "身份", read_only=True)
    res = tm.execute_tool("memory", {"action": "delete", "label": "persona"}, "tc1")
    assert "read-only" in res.text.lower()
    assert bm.get_block_by_label("persona") is not None


def test_memory_delete_removes_block():
    tm, bm, _ = _tools()
    bm.create_block("feedback_testing", "规则")
    res = tm.execute_tool("memory", {"action": "delete", "label": "feedback_testing"}, "tc1")
    assert "Deleted" in res.text
    assert bm.get_block_by_label("feedback_testing") is None


def test_memory_create_duplicate_label_errors():
    tm, bm, _ = _tools()
    bm.create_block("persona", "v1")
    res = tm.execute_tool("memory", {"action": "create", "label": "persona", "value": "v2"}, "tc1")
    assert "already exists" in res.text.lower()
    assert len([b for b in bm.list_blocks() if b.label == "persona"]) == 1


def test_memory_rename_preserves_metadata():
    tm, bm, _ = _tools()
    bm.create_block("persona", "身份", read_only=True, limit=500, description="desc")
    res = tm.execute_tool("memory", {"action": "rename", "label": "persona", "value": "human"}, "tc1")
    assert "human" in res.text
    b = bm.get_block_by_label("human")
    assert b is not None
    assert b.value == "身份"
    assert b.read_only is True
    assert b.limit == 500
    assert b.description == "desc"
    assert bm.get_block_by_label("persona") is None
