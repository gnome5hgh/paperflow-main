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
