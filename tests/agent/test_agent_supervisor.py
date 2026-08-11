"""Supervisor agent 冒烟测试：注册表装配 + INTENT 块消费冒烟（mock LLM）。"""
import pytest
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.conversation_state import ConversationState
from paperflow.core.llm import Message
from tests.agent.test_agent import make_mock_llm


@pytest.fixture
def supervisor_registry(tmp_path, monkeypatch):
    """真实 AgentRegistry（扫描 agents/），supervisor SKILL 必须被解析。"""
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"))
    monkeypatch.setattr(PaperFlowConfig, "from_env",
                        classmethod(lambda cls, config_path=None: cfg))
    agents_dir = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "agents")
    return AgentRegistry(agents_dir)


def test_supervisor_config_loads_with_supervisor_tools(supervisor_registry):
    cfg = supervisor_registry.get_config("supervisor")
    names = {t.name for t in cfg.tools}
    assert names == {"spawn_sub_agent", "ask_user_question"}
    assert "INTENT" in cfg.system_prompt          # 消费规则注入系统提示词


def test_supervisor_has_no_glob_grep(supervisor_registry):
    """Task 4：supervisor 不含 glob/grep——只调度不碰文件。

    文件访问（读/写/搜索）全部下放到文件型 agent（searcher/writer/
    qa-agent/reviewer）；supervisor 仅 2 个调度工具，权限最小化。
    此断言防将来向 supervisor 误加文件工具（它有 spawn 权限，绝不能有文件路径暴露）。"""
    config = supervisor_registry.get_config("supervisor")
    names = {t.name for t in config.tools}
    assert not ({"glob", "grep"} & names)


def test_supervisor_dispatch_smoke(supervisor_registry):
    """mock LLM：supervisor 先调 spawn 再返回——验证 ReAct 链路 + 工具真实可跑。"""
    tool_call = Message(role="assistant", content=None, tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "spawn_sub_agent", "arguments":
                     '{"agent_type": "searcher", "task": "搜索 circRNA"}'},
    }])
    # 3 条响应：① supervisor 调 spawn → ② child（searcher，同一 mock llm）返回
    # "子任务已执行" → ③ supervisor 汇总后返回"已搜索"。mock 列表 pop(0) 顺序消费，
    # 少一条会 IndexError。
    llm = make_mock_llm([
        tool_call,
        Message(role="assistant", content="子任务已执行"),
        Message(role="assistant", content="已搜索"),
    ])
    agent = Agent(llm=llm, agent_registry=supervisor_registry, agent_type="supervisor",
                  confirm_callback=lambda cr: True)
    # supervisor 前置钩子缺省关闭（未传 intent_pipeline/conversation）；spawn 的 child 是
    # 真实 searcher 装配但吃同一 mock llm → 返回"子任务已执行"，无网络。
    import asyncio
    text = asyncio.run(agent.run("搜索 circRNA 文献"))
    assert text == "已搜索"


def test_subagent_result_cross_turn_visible(supervisor_registry, tmp_path):
    """首轮 spawn 子 agent → 第二轮 messages 含首轮 spawn 的 tool 结果（子结果跨轮可见）。

    新机制下跨轮可见来自 MessageManager：run1 把 spawn 的 tool 结果落盘，run2 经
    _load_in_context 从 SQL 回放。child（spawn 构造）无 message_manager → 不持久化，
    不干扰 supervisor 的会话。
    """
    import asyncio  # noqa: F401  （函数内 import，与本文件 test_supervisor_dispatch_smoke 同风格）
    from paperflow.core.memory.orm.database import MemoryDB
    from paperflow.core.memory.services.message_manager import MessageManager
    from tests.agent.test_agent import make_capture_llm

    tool_call = Message(role="assistant", content=None, tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "spawn_sub_agent", "arguments":
                     '{"agent_type": "searcher", "task": "搜索 circRNA"}'},
    }])
    mm = MessageManager(MemoryDB(tmp_path / "memory.db"))
    capture = []
    # run1 消费 3 条（supervisor spawn → child → supervisor 汇总）；run2 消费 1 条
    llm = make_capture_llm([
        tool_call,
        Message(role="assistant", content="子任务已执行"),
        Message(role="assistant", content="已搜索"),
        Message(role="assistant", content="第二轮回答"),
    ], capture)
    agent = Agent(llm=llm, agent_registry=supervisor_registry, agent_type="supervisor",
                  confirm_callback=lambda cr: True, message_manager=mm,
                  session_id="sess_1")
    asyncio.run(agent.run("搜索 circRNA 文献"))
    asyncio.run(agent.run("这些论文有什么共同点"))
    # 防护（review Minor 5）：capture[3] 假设 run1 恰好 3 次 LLM 调用——装配/触发条件
    # 变化会让索引静默指错调用；先断言条数，跑偏时 loud fail 而非 silently check wrong call
    assert len(capture) >= 4
    contents = [m.content for m in capture[3]]                # run2 的 LLM 调用
    # 子串命中（非精确 in）：spawn 的 tool 结果是 SubAgentResult 的 JSON 序列化
    # （"summary" 字段含"子任务已执行"），裸文本精确匹配会误判——验证跨轮回放即可。
    assert any(c and "子任务已执行" in c for c in contents)    # run1 spawn 的 tool 结果被回放


def _make_intent_pipeline():
    """真实意图管线：FakeEmbedder + 已标定 routes（离线可跑，诊断同款）。"""
    from paperflow.core.intent.pipeline import IntentPipeline
    from paperflow.core.intent.hybrid_router import HybridRouter
    from paperflow.core.intent.route_loader import load_routes
    from paperflow.rag.embedder import FakeEmbedder
    router = HybridRouter(encoder=FakeEmbedder(), routes=load_routes(), alpha=0.6)
    structured = MagicMock()
    async def extract(prompt, schema, fallback=None):
        return fallback()
    structured.extract = extract
    return IntentPipeline(router=router, structured=structured)


def _make_supervisor_agent(supervisor_registry, llm, **kwargs):
    """装配真实 supervisor Agent：意图管线 + 会话，额外装配经 kwargs 透传。

    三个集成测试共用的构造块。记忆工具（block_manager/memory_tools）、
    ask_user_callback 等按测试需要经 kwargs 传给 Agent，缺省不装配。
    """
    return Agent(llm=llm, agent_registry=supervisor_registry,
                 agent_type="supervisor",
                 confirm_callback=lambda cr: True,
                 intent_enabled=True,
                 intent_pipeline=_make_intent_pipeline(),
                 conversation=ConversationState(),
                 **kwargs)


def test_statement_direction_intent_block_task_requested_false(supervisor_registry):
    """陈述方向（原失败句）→ INTENT 块携带 task_requested=false。

    supervisor 据此 memory_insert + ask_user_question，不 spawn searcher——
    信号确定性到达 ReAct 上下文，不靠 LLM 碰运气。
    """
    import asyncio
    from tests.agent.test_agent import make_capture_llm
    capture = []
    llm = make_capture_llm(
        [Message(role="assistant", content="好的")], capture)
    agent = _make_supervisor_agent(supervisor_registry, llm)
    asyncio.run(agent.run(
        "我的课题是做一个circRNA关联预测框架，"
        "同时预测circRNA-疾病/药物/miRNA的关联。这是我的目前方向"))
    blocks = [m.content for m in capture[0]
              if m.content and m.content.startswith("INTENT:")]
    assert len(blocks) == 1
    assert '"task_requested":false' in blocks[0]


def test_task_direction_intent_block_task_requested_true(supervisor_registry):
    """真任务 → INTENT 块携带 task_requested=true（正常派发路径）。"""
    import asyncio
    from tests.agent.test_agent import make_capture_llm
    capture = []
    llm = make_capture_llm(
        [Message(role="assistant", content="好的")], capture)
    agent = _make_supervisor_agent(supervisor_registry, llm)
    asyncio.run(agent.run("帮我搜索circRNA关联预测的最新论文"))
    blocks = [m.content for m in capture[0]
              if m.content and m.content.startswith("INTENT:")]
    assert len(blocks) == 1
    assert '"task_requested":true' in blocks[0]


def test_statement_direction_records_and_asks(supervisor_registry, tmp_path):
    """行为锁（task_requested=false）：memory_insert 落 human 块 + ask_user_question 真实询问。

    既有 INTENT 块断言只证明信号到达 ReAct 上下文，不证明 supervisor 真的执行
    memory_insert + ask_user_question——本测试让 mock LLM 真实发出这两个工具调用，
    断言 human 块被写入、ask 回调被调用并记录问题。memory 工具是框架级注入进工具面的
    （agent.py 经 memory_tools 注入，非 supervisor 注册），此契约无测试锁定会静默回归。
    """
    import asyncio
    from paperflow.core.memory.orm.database import MemoryDB
    from paperflow.core.memory.schemas.memory import Memory
    from paperflow.core.memory.services.block_manager import BlockManager
    from paperflow.core.memory.services.tool_manager import ToolManager
    from tests.agent.test_agent import make_capture_llm

    # 记忆装配（仿 test_agent_memory._agent）：ToolManager 播种 → memory_tools 注入
    db = MemoryDB(tmp_path / "memory.db")
    bm = BlockManager(db)
    tm = ToolManager(db)
    tm.bind(bm, None, None, agent_id="sess_1")
    tm.upsert_base_tools()
    bm.create_block("persona", "身份")
    # memory_insert 对缺失块硬失败，human 块必须预先播种
    bm.create_block("human", "用户正在研究 circRNA 关联预测")
    memory = Memory(blocks=bm.list_blocks())

    # ask_user_question 的回调：记录问题并返回应答文本
    asked: list[str] = []
    def ask_cb(question: str) -> str:
        asked.append(question)
        return "我明白了，先记住这个方向。"

    tool_calls = Message(role="assistant", content=None, tool_calls=[
        {"id": "m1", "type": "function",
         "function": {"name": "memory_insert",
                      "arguments": '{"label": "human", "new_string": "课题方向：circRNA关联预测框架"}'}},
        {"id": "m2", "type": "function",
         "function": {"name": "ask_user_question",
                      "arguments": '{"question": "接下来希望我如何推进这个课题？"}'}},
    ])
    capture = []
    llm = make_capture_llm(
        [tool_calls, Message(role="assistant", content="好的")], capture)
    agent = _make_supervisor_agent(supervisor_registry, llm, memory=memory,
                                   block_manager=bm, memory_tools=tm.list_tools(),
                                   ask_user_callback=ask_cb)
    text = asyncio.run(agent.run(
        "我的课题是做一个circRNA关联预测框架，"
        "同时预测circRNA-疾病/药物/miRNA的关联。这是我的目前方向"))
    assert text == "好的"
    # 因果链：INTENT 块携带 task_requested=false，supervisor 据此记录+询问
    blocks = [m.content for m in capture[0]
              if m.content and m.content.startswith("INTENT:")]
    assert len(blocks) == 1
    assert '"task_requested":false' in blocks[0]
    # memory_insert 真实执行：human 块经 block_manager 查证已写入
    human = bm.get_block_by_label("human")
    assert human is not None and "课题方向：circRNA关联预测框架" in human.value
    # ask_user_question 真实执行：回调被调用并记录问题
    assert asked == ["接下来希望我如何推进这个课题？"]
