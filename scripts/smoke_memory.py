"""记忆系统冒烟测试——无 LLM、无 API key，直接驱动核心组件验证链路。

覆盖（每项都断言）：
  1. MemoryDB 建表（4 张表）
  2. GitEnabledBlockManager 建块 → MemFS markdown 投影 + git commit + 自动索引
  3. Memory.compile() 渲染 system/ 块 + 索引
  4. MessageManager 落盘/回放/检索（Recall）
  5. PassageManager 写入/检索/软删除（archival）
  6. ToolManager 播种 9 个记忆工具 + 执行 memory_replace / archival_memory_insert / conversation_search
  7. Sleeptime run_once_if_due 不崩

用法：conda run -n paperflow python scripts/smoke_memory.py
退出码：0 = 全部通过；1 = 有失败
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="paperflow-memory-smoke-"))
MEM = TMP / "memory"


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  ✅ {name}")
    except Exception as e:
        print(f"  ❌ {name}: {e}")
        sys.exit(1)


def main() -> None:
    print(f"冒烟目录: {TMP}")

    from paperflow.core.memory.orm.database import MemoryDB
    from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
    from paperflow.core.memory.services.message_manager import MessageManager
    from paperflow.core.memory.services.passage_manager import PassageManager
    from paperflow.core.memory.services.tool_manager import ToolManager
    from paperflow.core.memory.schemas.memory import Memory
    from paperflow.core.memory.sleeptime import Sleeptime
    from paperflow.core.llm import Message as WireMessage

    db = MemoryDB(MEM / "memory.db")

    def _tables():
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"blocks", "block_history", "messages", "archival_passages"} <= names
    check("MemoryDB 建表（blocks/block_history/messages/archival_passages）", _tables)

    bm = GitEnabledBlockManager(db, memfs_dir=MEM)
    mm = MessageManager(db)
    pm = PassageManager(db)

    def _blocks():
        b = bm.create_block("persona", "你是学术研究助手")
        bm.create_block("feedback_testing", "测试规则")
        assert bm.get_block_by_label("persona").value == "你是学术研究助手"
        assert (MEM / "system" / "persona.md").exists(), "persona 投影文件"
        assert (MEM / "feedback_testing.md").exists(), "非 system 投影文件"
        assert (MEM / "memory_filesystem.md").exists(), "自动索引"
        assert len(bm._git_log()) >= 2, "git commit"
    check("BlockManager 建块 → MemFS 投影 + git commit + 自动索引", _blocks)

    def _compile():
        mem = Memory(blocks=bm.list_blocks())
        out = mem.compile(index_text=(MEM / "memory_filesystem.md").read_text(encoding="utf-8"))
        assert "persona" in out and "你是学术研究助手" in out, "system/ 块内容"
        assert "feedback_testing" in out, "索引含非 system 块"
    check("Memory.compile() 渲染 system/ 块 + 索引", _compile)

    def _messages():
        mm.add_message("sess_1", WireMessage(role="user", content="帮我读一篇 circRNA 论文"))
        mm.add_message("sess_1", WireMessage(role="assistant", content="已找到，正在阅读"))
        got = mm.get_in_context_messages("sess_1")
        assert len(got) == 2 and "circRNA" in got[0].content, "落盘+回放"
        hits = mm.search_messages("sess_1", "circRNA")
        assert len(hits) == 1, "search_messages 检索"
    check("MessageManager 落盘/回放/检索（Recall）", _messages)

    def _passages():
        p = pm.insert_passage("sess_1", "GraphCL 是图对比学习模型", tags=["paper"])
        assert pm.agent_passage_size("sess_1") == 1, "写入"
        found = pm.search_passages("sess_1", "", tags=["paper"])
        assert len(found) == 1 and "GraphCL" in found[0].text, "按 tags 检索"
        pm.delete_passage(p.id)
        assert pm.agent_passage_size("sess_1") == 0, "软删除"
    check("PassageManager 写入/检索/软删除（archival）", _passages)

    def _tools():
        tm = ToolManager(db)
        tm.bind(bm, pm, mm, agent_id="sess_1")
        tm.upsert_base_tools()
        names = {t.name for t in tm.list_tools()}
        assert {"memory_replace", "memory_insert", "memory_rethink", "memory_finish_edits",
                "memory", "memory_apply_patch", "archival_memory_insert",
                "archival_memory_search", "conversation_search"} <= names, "9 个工具"
        r = tm.execute_tool("memory_replace", {
            "label": "feedback_testing", "old_string": "测试规则", "new_string": "新规则"}, "tc1")
        assert "新规则" in r.text, "memory_replace 生效"
        assert "新规则" in bm.get_block_by_label("feedback_testing").value, "块已更新"
        tm.execute_tool("archival_memory_insert", {"content": "用户偏好: 关注 circRNA", "tags": ["user"]}, "tc2")
        assert pm.agent_passage_size("sess_1") == 1, "archival 写入工具"
        r2 = tm.execute_tool("conversation_search", {"query": "circRNA"}, "tc3")
        assert "circRNA" in r2.text, "conversation_search 工具"
    check("ToolManager 播种 9 工具 + memory_replace/archival/conversation 执行", _tools)

    def _sleeptime():
        import asyncio
        from paperflow.core.memory.schemas.agent import AgentState
        from paperflow.core.memory.schemas.memory import Memory as MemCls
        st = AgentState(agent_id="sess_1", memory=MemCls(blocks=bm.list_blocks()))
        sl = Sleeptime(st, bm, pm, mm, structured=None, enable=True,
                       frequency=1, min_interval_s=0, max_entries=20)
        asyncio.run(sl.run_once_if_due())   # 无 LLM：fast-path 或优雅降级，不崩
    check("Sleeptime run_once_if_due 不崩", _sleeptime)

    print("\n🎉 记忆系统冒烟全部通过")
    print(f"投影目录示例: {MEM / 'system' / 'persona.md'}")
    print(f"memory_filesystem.md: {MEM / 'memory_filesystem.md'}")


if __name__ == "__main__":
    main()
