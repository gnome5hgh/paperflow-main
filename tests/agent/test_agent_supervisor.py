"""Supervisor agent 冒烟测试：注册表装配 + INTENT 块消费冒烟（mock LLM）。

K1-K10 是确定性路由代码路径回归锁（FakeEmbedder 稠密信号近常数，路由质量
由 Task 8 verify_intent 用真实 bge 验证）——615 全绿不代表"路由可用"。"""
import pytest
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.llm import Message
from paperflow.core.memory.tools import get_memory_tools
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
    """supervisor 工具面 = 2 调度工具 + 13 记忆工具（Task 8 后经 get_memory_tools 注入）。"""
    cfg = supervisor_registry.get_config("supervisor")
    names = {t.name for t in cfg.tools}
    assert names == {"spawn_sub_agent", "ask_user_question"} | {t.name for t in get_memory_tools()}
    assert "INTENT" in cfg.system_prompt          # 消费规则注入系统提示词


def test_supervisor_has_no_glob_grep(supervisor_registry):
    """Task 4：supervisor 不含 glob/grep——只调度不碰文件。

    文件访问（读/写/搜索）全部下放到文件型 agent（searcher/writer/
    qa-agent/reviewer）；supervisor 的调度工具仅 spawn/ask_user（另注入 13 个
    记忆工具，无文件访问），权限最小化。此断言防将来向 supervisor 误加文件工具
    （它有 spawn 权限，绝不能有文件路径暴露）。"""
    config = supervisor_registry.get_config("supervisor")
    names = {t.name for t in config.tools}
    assert not ({"glob", "grep"} & names)


def test_supervisor_spawns_writer_outline_mode(agent_registry):
    """supervisor 工具面含 spawn_sub_agent；write_outline 由 SKILL 指导传 mode=outline（文档断言）。"""
    config = agent_registry.get_config("supervisor")
    names = {t.name for t in config.tools}
    assert "spawn_sub_agent" in names


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
    """真实意图管线：FakeEmbedder + load_routes（离线可跑，诊断同款）。"""
    from paperflow.core.intent.pipeline import IntentPipeline
    from paperflow.core.intent.routing.router import HybridRouter
    from paperflow.core.intent.routing.route_loader import load_routes
    from paperflow.rag.encoders.embedder import FakeEmbedder
    router = HybridRouter(encoder=FakeEmbedder(), routes=load_routes(), alpha=0.6)
    structured = MagicMock()
    async def extract(prompt, schema, fallback=None):
        return fallback()
    structured.extract = extract
    return IntentPipeline(router=router, structured=structured)


def _run_supervisor_task(query, supervisor_registry):
    """跑一次 supervisor run，返回 (INTENT 块文本, capture[0])。

    supervisor_registry 由各测试显式传入（fixture 值只在测试函数局部作用域生效，
    模块级 helper 取不到，直接引用会拿到 pytest 的 fixture 定义对象）。"""
    import asyncio
    from paperflow.core.agent import Agent
    from paperflow.core.intent.conversation_state import ConversationState
    from tests.agent.test_agent import make_capture_llm
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="好的")], capture)
    agent = Agent(llm=llm, agent_registry=supervisor_registry,
                  agent_type="supervisor", confirm_callback=lambda cr: True,
                  intent_enabled=True, intent_pipeline=_make_intent_pipeline(),
                  conversation=ConversationState())
    asyncio.run(agent.run(query))
    block = next(m.content for m in capture[0]
                 if m.content and m.content.startswith("INTENT:"))
    return block, capture


def test_k1_statement_routes_to_set_research_topic(supervisor_registry):
    """原 bug 回归线：陈述方向必须路由到 set_research_topic，不得落 manage_memory/search_paper。"""
    block, _ = _run_supervisor_task(
        "我的课题是做一个circRNA关联预测框架，同时预测circRNA-疾病/药物/miRNA的关联。这是我的目前方向",
        supervisor_registry)
    assert '"intent_type":"set_research_topic"' in block


def test_k2_to_k9_routing(supervisor_registry):
    """K2-K9：各意图代表句路由到正确意图。"""
    cases = [
        ("帮我搜索circRNA关联预测的最新论文", "search_paper"),
        ("太老了", "refine_query"),
        ("换个话题，看看图对比学习", "switch_topic"),
        ("你好啊", "chitchat"),
        ("帮我代写论文", "out_of_scope"),
        ("你能干什么", "help"),
        ("这个回答不对", "feedback"),
        ("把这篇加入待读清单", "manage_memory"),
    ]
    for query, expected in cases:
        block, _ = _run_supervisor_task(query, supervisor_registry)
        assert f'"intent_type":"{expected}"' in block, f"{query} → 期望 {expected}"


def test_k10_gate_blocks_spawn_for_non_dispatch(supervisor_registry):
    """K10 门禁交叉：chitchat 轮 supervisor 若尝试 spawn 会被工具拒绝（代码级）。"""
    import asyncio
    from paperflow.core.agent import Agent
    from paperflow.core.intent.conversation_state import ConversationState
    from tests.agent.test_agent import make_capture_llm
    spawn_call = Message(role="assistant", content=None, tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "spawn_sub_agent",
                     "arguments": '{"agent_type": "searcher", "task": "搜"}'}}])
    capture = []
    llm = make_capture_llm([spawn_call, Message(role="assistant", content="直接回复")], capture)
    agent = Agent(llm=llm, agent_registry=supervisor_registry,
                  agent_type="supervisor", confirm_callback=lambda cr: True,
                  intent_enabled=True, intent_pipeline=_make_intent_pipeline(),
                  conversation=ConversationState())
    asyncio.run(agent.run("你好啊"))   # chitchat → 门禁应拒绝 spawn
    # capture[1] = 第二轮（工具结果回放后），含 spawn 的 ToolResult
    texts = "".join(m.content for m in capture[1] if m.content)
    assert "不派发" in texts
