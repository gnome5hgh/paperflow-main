"""Supervisor agent 冒烟测试：注册表装配 + INTENT 块消费冒烟（mock LLM）。"""
import pytest
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.llm import Message
from tests.test_agent import make_mock_llm


@pytest.fixture
def supervisor_registry(tmp_path, monkeypatch):
    """真实 AgentRegistry（扫描 agents/），supervisor SKILL 必须被解析。"""
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"))
    monkeypatch.setattr(PaperFlowConfig, "from_env",
                        classmethod(lambda cls, config_path=None: cfg))
    agents_dir = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "agents")
    return AgentRegistry(agents_dir)


def test_supervisor_config_loads_with_four_tools(supervisor_registry):
    cfg = supervisor_registry.get_config("supervisor")
    names = {t.name for t in cfg.tools}
    assert names == {"spawn_sub_agent", "parallel_spawn", "aggregate_results", "ask_user"}
    assert "INTENT" in cfg.system_prompt          # 消费规则注入系统提示词


def test_supervisor_has_no_glob_grep(supervisor_registry):
    """Task 4：supervisor 不含 glob/grep——只调度不碰文件。

    文件访问（读/写/搜索）全部下放到文件型 agent（search-paper/generate-note/
    answer-question/reviewer）；supervisor 仅 4 个调度工具，权限最小化。
    此断言防将来向 supervisor 误加文件工具（它有 spawn 权限，绝不能有文件路径暴露）。"""
    config = supervisor_registry.get_config("supervisor")
    names = {t.name for t in config.tools}
    assert not ({"glob", "grep"} & names)


def test_supervisor_dispatch_smoke(supervisor_registry):
    """mock LLM：supervisor 先调 spawn 再返回——验证 ReAct 链路 + 工具真实可跑。"""
    tool_call = Message(role="assistant", content=None, tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "spawn_sub_agent", "arguments":
                     '{"agent_type": "search-paper", "task": "搜索 circRNA"}'},
    }])
    # 3 条响应：① supervisor 调 spawn → ② child（search-paper，同一 mock llm）返回
    # "子任务已执行" → ③ supervisor 汇总后返回"已搜索"。mock 列表 pop(0) 顺序消费，
    # 少一条会 IndexError。
    llm = make_mock_llm([
        tool_call,
        Message(role="assistant", content="子任务已执行"),
        Message(role="assistant", content="已搜索"),
    ])
    agent = Agent(llm=llm, agent_registry=supervisor_registry, agent_type="supervisor",
                  confirm_callback=lambda cr: True)
    # supervisor 前置钩子缺省关闭（未传 intent_pipeline/session）；spawn 的 child 是
    # 真实 search-paper 装配但吃同一 mock llm → 返回"子任务已执行"，无网络。
    import asyncio
    text = asyncio.run(agent.run("搜索 circRNA 文献"))
    assert text == "已搜索"


def test_subagent_result_cross_turn_visible(supervisor_registry):
    """首轮 spawn 子 agent → 第二轮 messages 含首轮 spawn 的 tool 结果（子结果跨轮可见）。"""
    import asyncio  # noqa: F401  （函数内 import，与本文件 test_supervisor_dispatch_smoke 同风格）
    from unittest.mock import MagicMock
    from paperflow.core.memory.context_compressor import ContextCompressor
    from paperflow.core.memory.context_config import ContextConfig
    from tests.test_agent import make_capture_llm

    tool_call = Message(role="assistant", content=None, tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "spawn_sub_agent", "arguments":
                     '{"agent_type": "search-paper", "task": "搜索 circRNA"}'},
    }])
    structured = MagicMock()
    async def extract(prompt, schema, fallback=None):
        return fallback()
    structured.extract = extract
    comp = ContextCompressor(ContextConfig(), MagicMock(context_window=65536), structured)
    capture = []
    # run1 消费 3 条（supervisor spawn → child → supervisor 汇总）；run2 消费 1 条
    llm = make_capture_llm([
        tool_call,
        Message(role="assistant", content="子任务已执行"),
        Message(role="assistant", content="已搜索"),
        Message(role="assistant", content="第二轮回答"),
    ], capture)
    agent = Agent(llm=llm, agent_registry=supervisor_registry, agent_type="supervisor",
                  confirm_callback=lambda cr: True, compressor=comp)
    asyncio.run(agent.run("搜索 circRNA 文献"))
    asyncio.run(agent.run("这些论文有什么共同点"))
    # 防护（review Minor 5）：capture[3] 假设 run1 恰好 3 次 LLM 调用——装配/触发条件
    # 变化会让索引静默指错调用；先断言条数，跑偏时 loud fail 而非 silently check wrong call
    assert len(capture) >= 4
    contents = [m.content for m in capture[3]]                # run2 的 LLM 调用
    # 子串命中（非精确 in）：spawn 的 tool 结果是 SubAgentResult 的 JSON 序列化
    # （"summary" 字段含"子任务已执行"），裸文本精确匹配会误判——验证跨轮回放即可。
    assert any(c and "子任务已执行" in c for c in contents)    # run1 spawn 的 tool 结果被回放
