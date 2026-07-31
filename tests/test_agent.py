# tests/test_agent.py
"""
Agent ReAct 循环的单元测试。

本测试文件使用 mock LLM 验证 Agent 的三个核心层：

1. **工具路由层（TestExecTool）** — _exec_tool 的方法分派逻辑：
   正确路由到目标工具、JSON 解析错误处理、未知工具名处理

2. **ReAct 循环层（TestAgentRun）** — run() 的循环控制逻辑：
   无 tool_call 直接返回、tool-call 循环执行、max_turns 安全阀

所有测试不依赖网络和真实 LLM，通过 make_mock_llm / make_mock_registry
提供可控的 mock 对象，保证测试的确定性和速度。
"""

from unittest.mock import MagicMock

import pytest

from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.llm import Message


# ─── Mock 工厂函数 ────────────────────────────────────────────────


def make_mock_llm(responses: list[Message]):
    """
    创建一个 mock LLMClient，其 chat() 按顺序返回预设的 Message。

    采用列表的 pop(0) 模式：每次 chat 调用消费列表第一项，
    模拟 LLM 的多轮对话行为（第一轮返回 tool_calls，
    第二轮收到 tool result 后返回最终回答）。

    :param responses: 预设的 Message 序列，按调用顺序排列
    :returns: MagicMock 包装的 LLMClient，chat() 按顺序返回 responses
    """
    mock = MagicMock()

    async def chat(messages, tools=None, tool_choice="auto"):
        # 每次调用消费预设序列的第一条消息
        return responses.pop(0)

    mock.chat = chat
    mock.model = "mock"
    return mock


def make_mock_registry(tools, system_prompt="test prompt"):
    """
    创建一个 mock AgentRegistry，get_config() 返回预设的 AgentConfig。

    :param tools: 注入的 Tool 实例列表（对应 TestAgent 的工具集）
    :param system_prompt: 注入的系统提示词
    :returns: MagicMock(spec=AgentRegistry)，确保只调用注册表定义的接口
    """
    registry = MagicMock(spec=AgentRegistry)
    registry.get_config.return_value = AgentConfig(
        name="test",
        system_prompt=system_prompt,
        tools=tools,
    )
    return registry


# ─── TestExecTool：工具执行层的单元测试 ─────────────────────────────


class TestExecTool:
    """
    测试 Agent._exec_tool 的工具路由和错误处理。

    _exec_tool 是 Agent 内部的确定性逻辑（不依赖 LLM），
    这些测试直接调用它，无需 mock 整个 ReAct 循环。
    """

    @pytest.mark.asyncio
    async def test_routes_to_correct_tool(self):
        """
        验证 _exec_tool 能根据 tool_call 中的 name 正确查找并执行目标工具。

        给定：
          - 注册表中有一个 name="echo" 的 MockEchoTool
          - tool_call 请求 name="echo" 且 arguments='{"message": "hello"}'
        预期：
          - 返回 ToolResult(text="Echo: hello")
        """
        from tests.conftest import MockEchoTool

        tool = MockEchoTool()
        registry = make_mock_registry([tool])

        # mock LLM 不需要实际调用（_exec_tool 不经过 LLM）
        llm = make_mock_llm([Message(role="assistant", content="Done.")])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        # 直接测试 _exec_tool —— 不触发 ReAct 循环
        result = await agent._exec_tool({
            "id": "call_1",
            "function": {"name": "echo", "arguments": '{"message": "hello"}'},
        })
        assert result.text == "Echo: hello"

    @pytest.mark.asyncio
    async def test_returns_error_on_json_decode_error(self):
        """
        验证 LLM 生成的非法 JSON 参数被优雅处理。

        给定：
          - arguments 字段不是合法 JSON（"{bad json"）
        预期：
          - 返回 ToolResult 包含 "Tool argument parse error"
          - 不抛出异常（采用"degrade to text"策略）
        """
        registry = make_mock_registry([])
        llm = make_mock_llm([])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        # arguments 缺少闭合引号和括号 → json.loads 抛出 JSONDecodeError
        result = await agent._exec_tool({
            "id": "call_1",
            "function": {"name": "any", "arguments": "{bad json"},
        })
        assert "Tool argument parse error" in result.text

    @pytest.mark.asyncio
    async def test_returns_error_on_unknown_tool(self):
        """
        验证 LLM 请求不存在的工具时返回友好错误信息。

        给定：
          - 注册表中无任何 Tool
          - tool_call 请求 name="nonexistent"
        预期：
          - 返回 ToolResult 包含 "Unknown tool" 和可用工具列表
          - 不抛出 KeyError（使用 .get() + None-check 而非 [] 索引）
        """
        registry = make_mock_registry([])
        llm = make_mock_llm([])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = await agent._exec_tool({
            "id": "call_1",
            "function": {"name": "nonexistent", "arguments": "{}"},
        })
        assert "Unknown tool" in result.text


# ─── TestAgentRun：ReAct 循环层的单元测试 ──────────────────────────


class TestAgentRun:
    """
    测试 Agent.run() 的 ReAct 循环控制逻辑。

    这些测试使用 mock LLM 注入预设的多轮对话序列，
    验证 Agent 在不同 LLM 响应模式下的行为是否正确。
    """

    @pytest.mark.asyncio
    async def test_returns_content_when_no_tool_calls(self):
        """
        验证最简单的 ReAct 路径：LLM 直接返回文本回答，不调用任何工具。

        给定：
          - LLM 第一轮返回纯文本 "Hello, I am the agent."（无 tool_calls）
        预期：
          - run() 直接返回该文本，循环在 1 轮内结束
          - 不尝试执行任何工具
        """
        from tests.conftest import MockEchoTool

        registry = make_mock_registry([MockEchoTool()])

        # mock LLM：只返回一条消息（无 tool_calls），模拟 LLM 直接回答
        llm = make_mock_llm([
            Message(role="assistant", content="Hello, I am the agent.")
        ])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = await agent.run("Hi!")
        assert result == "Hello, I am the agent."

    @pytest.mark.asyncio
    async def test_calls_tool_and_continues(self):
        """
        验证完整的"调用工具 → 获取结果 → 继续推理"路径。

        给定：
          - 第 1 轮 LLM 返回 tool_calls → Agent 执行 echo 工具
          - 第 2 轮 LLM 收到 tool result 后返回最终文本

        预期：
          - run() 在第 1 轮执行工具，第 2 轮返回最终结果
          - 最终结果包含 LLM 对 tool 输出的引用
        """
        from tests.conftest import MockEchoTool

        registry = make_mock_registry([MockEchoTool()])

        # 模拟两轮对话：
        # 轮1：LLM 请求调用 echo(message="hello")
        # 轮2：LLM 收到 "Echo: hello" 后给出最终回答
        llm = make_mock_llm([
            Message(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "arguments": '{"message": "hello"}',
                    },
                }],
            ),
            Message(
                role="assistant",
                content="The tool said: Echo: hello",
            ),
        ])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = await agent.run("Echo hello!")
        assert result == "The tool said: Echo: hello"

    @pytest.mark.asyncio
    async def test_raises_max_turns_exceeded(self):
        """
        验证 max_turns 安全阀正确触发。

        给定：
          - max_turns=3
          - LLM 每轮都返回 tool_calls（永远不会给出最终回答）

        预期：
          - 第 3 轮结束后抛出 MaxTurnsExceeded 异常
          - 不会无限循环

        这是 Agent 唯一的"不可恢复"异常 —— LLM 陷入了
        无法自主退出的 tool-calling 循环。
        """
        registry = make_mock_registry([])

        # 生成 25 条 tool_call 消息（远超过 max_turns=3）
        # Agent 每轮消费一条，第 3 轮结束后触发 MaxTurnsExceeded
        llm = make_mock_llm([
            Message(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{}"},
                }],
            )
            for i in range(25)
        ])
        agent = Agent(
            llm=llm, agent_registry=registry, agent_type="test", max_turns=3
        )

        with pytest.raises(MaxTurnsExceeded):
            await agent.run("Loop forever!")
