"""CLI 组装 smoke test：managers 装配链 + Agent 新签名 + Sleeptime。

test_main_assembly_no_typeerror 是 T11 遗留 TypeError（StructuredOutput(llm,
store=store)）的回归锁——main() 走通新 Letta 服务层即证明断点已消失。
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from paperflow.cli import main
from paperflow.config import PaperFlowConfig
from paperflow.core.memory.orm import block as block_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.passage_manager import PassageManager
from paperflow.core.memory.services.archive_manager import ArchiveManager
from paperflow.core.memory.services.tool_manager import ToolManager
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.rag.encoders.embedder import FakeEmbedder

#: 真实 agents 目录（main() 装配用真实 AgentRegistry 扫描）
_AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"


def test_assembly_chain():
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    bm = GitEnabledBlockManager(db, memfs_dir=tmp / "memory")
    mm = MessageManager(db)
    pm = PassageManager(db)
    arm = ArchiveManager(db, pm)
    tm = ToolManager(db)
    tm.bind(bm, pm, mm, agent_id="sess_1")
    tm.upsert_base_tools()
    am = AgentManager(db, bm, mm)
    st = am.create_agent("sess_1")
    assert st.agent_id == "sess_1"
    assert {t.name for t in tm.list_tools()} >= {"memory_replace", "conversation_search"}
    # MemFS 投影目录已建
    assert (tmp / "memory").exists()


def test_main_assembly_no_typeerror(monkeypatch):
    """T11 回归：main() 走通 Letta 服务层组装（StructuredOutput store= 断点消失）。

    monkeypatch from_env / LLMClient / _repl 绕开 api_key 守卫与交互循环；
    _rag_embedder 换 FakeEmbedder——HybridRouter 构造时会对路由语料编码，真实
    bge 会联网加载模型（测试环境不应触发）。断言 memory.db 已建（服务层真实挂载）。
    """
    tmp = Path(tempfile.mkdtemp())
    cfg = PaperFlowConfig()
    cfg.workspace = str(tmp)
    cfg.agents_dir = str(_AGENTS_DIR)
    cfg.sleeptime_enable = False

    async def _noop_repl(*a, **k):
        return None

    monkeypatch.setattr("paperflow.cli.PaperFlowConfig.from_env",
                        staticmethod(lambda: cfg))
    monkeypatch.setattr("paperflow.cli.LLMClient",
                        lambda *a, **k: MagicMock())
    monkeypatch.setattr("paperflow.cli._rag_embedder",
                        lambda config: FakeEmbedder())
    monkeypatch.setattr("paperflow.cli._repl", _noop_repl)

    main()  # 无异常 = 组装成功（T11 前在此抛 TypeError）
    assert (tmp / "memory" / "memory.db").exists()


def test_main_seeds_default_blocks(monkeypatch):
    """核心记忆生命周期：main() 装配后 persona/human 默认块已播种、投影文件已生成。"""
    tmp = Path(tempfile.mkdtemp())
    cfg = PaperFlowConfig()
    cfg.workspace = str(tmp)
    cfg.agents_dir = str(_AGENTS_DIR)
    cfg.sleeptime_enable = False

    async def _noop_repl(*a, **k):
        return None

    monkeypatch.setattr("paperflow.cli.PaperFlowConfig.from_env",
                        staticmethod(lambda: cfg))
    monkeypatch.setattr("paperflow.cli.LLMClient",
                        lambda *a, **k: MagicMock())
    monkeypatch.setattr("paperflow.cli._rag_embedder",
                        lambda config: FakeEmbedder())
    monkeypatch.setattr("paperflow.cli._repl", _noop_repl)

    main()
    db = MemoryDB(tmp / "memory" / "memory.db")
    labels = {r["label"] for r in block_orm.select_blocks(db)}
    assert {"persona", "human"} <= labels
    assert (tmp / "memory" / "system" / "persona.md").exists()


def test_ask_user_recorder_persists_q_and_a():
    """ask_user 问答（含子 agent 的）经 recorder 落 messages 表 → Sleeptime 可整合进 human 块。"""
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    mm = MessageManager(db)
    recorder = mm.make_ask_recorder(lambda q: "研究生", "sess_1")
    answer = recorder("你的身份？")
    assert answer == "研究生"                       # 答案原样透传（不阻断提问）
    msgs = mm.get_messages_by_agent_id("sess_1")
    assert len(msgs) == 1
    assert msgs[0].role.value == "user"
    assert "你的身份？" in msgs[0].content
    assert "研究生" in msgs[0].content


def test_ask_user_recorder_failsafe_passthrough():
    """记录失败不阻断提问：answer 仍返回（fail-safe）。"""
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    mm = MessageManager(db)
    mm.add_message = MagicMock(side_effect=RuntimeError("db down"))
    recorder = mm.make_ask_recorder(lambda q: "答案", "sess_1")
    assert recorder("问题") == "答案"
