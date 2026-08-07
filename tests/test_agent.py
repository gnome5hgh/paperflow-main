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

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.intent.intent_schema import IntentType
from paperflow.core.llm import Message
from paperflow.core.session import Session
from paperflow.core.tool import Tool, ToolResult


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

    @pytest.mark.asyncio
    async def test_no_stream_callback_skips_tool_event_formatting(self, monkeypatch):
        """零开销不变式回归（final review）：stream_callback=None 时 _exec_tool 不得
        求值 _format_tool_call（json.loads）——monkeypatch 为抛错来证明其未被调用。"""
        from tests.conftest import MockEchoTool

        def _boom(*a, **k):
            raise AssertionError("_format_tool_call 不应在无回调时被求值")
        monkeypatch.setattr("paperflow.core.agent._format_tool_call", _boom)
        registry = make_mock_registry([MockEchoTool()])
        llm = make_mock_llm([Message(role="assistant", content="Done.")])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = await agent._exec_tool({
            "id": "call_1",
            "function": {"name": "echo", "arguments": '{"message": "hello"}'},
        })
        assert result.text == "Echo: hello"


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


# ─── TestExecTool：工具执行线程化（Layer 2）─────────────────────────


class ThreadProbeTool(Tool):
    """
    线程探针工具 —— 返回当前执行线程名，用于验证 _exec_tool 是否把
    同步工具放到线程池执行（而非阻塞事件循环所在线程）。
    """

    name = "probe"
    description = "returns current thread name"
    parameters = {"type": "object", "properties": {}}

    def execute(self) -> ToolResult:
        # 返回当前线程名：事件循环跑在 MainThread，而线程池是 worker 线程
        return ToolResult(text=threading.current_thread().name)


@pytest.mark.asyncio
async def test_tool_executes_off_main_thread():
    """
    验证 _exec_tool 用 asyncio.to_thread 把同步工具放到线程池执行。

    给定：
      - ThreadProbeTool 返回当前执行线程名
    预期：
      - 工具执行线程不是 MainThread（事件循环所在线程）
        —— 说明重工具（CPU/网络密集）不会阻塞事件循环

    这是 Layer 4 parallel_spawn 并行与 Dream 后台任务能跑的前提：
    若工具同步执行会卡住事件循环，Agent 就无法同时处理多个工具。
    """
    tool = ThreadProbeTool()
    registry = make_mock_registry([tool])
    llm = make_mock_llm([Message(role="assistant", content="Done.")])
    agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

    # 直接测试 _exec_tool —— 不触发 ReAct 循环
    result = await agent._exec_tool({"function": {"name": "probe", "arguments": "{}"}})
    assert result.text != "MainThread"


# ─── 父引用注入（Layer 3）：needs_parent / attach_agent 钩子 ─────────


class ParentProbeTool(Tool):
    """声明 needs_parent 的工具：验证 Agent.__init__ 注入父引用。"""
    name = "parent_probe"
    description = "needs parent injection"
    parameters = {"type": "object", "properties": {}}
    needs_parent = True

    def execute(self) -> ToolResult:
        return ToolResult(text="ok")

    def get_parent(self):
        return getattr(self, "_parent", None)


def test_agent_injects_parent_to_optin_tool():
    tool = ParentProbeTool()
    registry = make_mock_registry([tool])
    llm = make_mock_llm([Message(role="assistant", content="Done.")])
    agent = Agent(llm=llm, agent_registry=registry, agent_type="test")
    assert agent.agent_registry is registry          # 新增属性
    assert tool.get_parent() is agent                 # opt-in 注入


def test_agent_does_not_inject_into_atomic_tools():
    from tests.conftest import MockEchoTool
    tool = MockEchoTool()
    registry = make_mock_registry([tool])
    llm = make_mock_llm([Message(role="assistant", content="Done.")])
    Agent(llm=llm, agent_registry=registry, agent_type="test")
    assert getattr(tool, "_parent", None) is None     # 原子工具不注入


# ─── 意图前置钩子（Layer 4）：intent_enabled 门控 + INTENT 块 + 澄清/降级 ──


class MockIntentPipeline:
    """mock 意图管线：记录调用参数，返回预设 IntentOutput 或抛异常。"""
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls: list[tuple] = []

    async def run(self, query, prev_intent=None, prev_user_input=""):
        self.calls.append((query, prev_intent, prev_user_input))
        if self.exc is not None:
            raise self.exc
        return self.result


def make_capture_llm(responses, capture):
    """mock LLM：把每次收到的 messages 追加进 capture，便于断言 INTENT 块注入。"""
    mock = MagicMock()
    async def chat(messages, tools=None, tool_choice="auto"):
        capture.append(messages)
        return responses.pop(0)
    mock.chat = chat
    mock.model = "mock"
    return mock


def _intent(intent_type, *, clarification=None):
    from paperflow.core.intent.intent_schema import IntentOutput, IntentStep
    return IntentOutput(intent_type=intent_type, confidence=0.9,
                        source=IntentStep.ROUTER, clarification=clarification)


class TestIntentGate:
    """intent_enabled 门控 + INTENT 块注入 + 澄清/降级分支。"""

    def test_disabled_skips_pipeline(self):
        capture = []
        pipeline = MockIntentPipeline(result=_intent(IntentType.SEARCH_PAPER))
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test")            # intent_enabled 缺省 False
        asyncio.run(agent.run("搜索 x"))
        assert pipeline.calls == []
        assert not any("INTENT:" in m.content for m in capture[0])

    def test_enabled_injects_intent_block(self):
        from paperflow.core.intent.intent_schema import IntentType
        capture = []
        pipeline = MockIntentPipeline(result=_intent(IntentType.SEARCH_PAPER))
        session = Session()
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test", intent_enabled=True,
                      intent_pipeline=pipeline, session=session)
        asyncio.run(agent.run("搜索 circRNA 文献"))
        assert pipeline.calls == [("搜索 circRNA 文献", None, "")]
        assert any("INTENT:" in m.content for m in capture[0])
        assert any("search_paper" in m.content for m in capture[0])

    def test_pipeline_receives_prev_context(self):
        capture = []
        pipeline = MockIntentPipeline(result=_intent(IntentType.SEARCH_PAPER))
        session = Session(prev_intent=IntentType.ASK_QUESTION, prev_user_input="上轮")
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test", intent_enabled=True,
                      intent_pipeline=pipeline, session=session)
        asyncio.run(agent.run("那第三篇呢"))
        assert pipeline.calls == [("那第三篇呢", IntentType.ASK_QUESTION, "上轮")]

    def test_clarification_short_circuits(self):
        capture = []
        pipeline = MockIntentPipeline(result=_intent(IntentType.ASK_QUESTION,
                                                     clarification="要搜索还是生成笔记？"))
        session = Session()
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test", intent_enabled=True,
                      intent_pipeline=pipeline, session=session)
        text = asyncio.run(agent.run("整理这篇"))
        assert text == "要搜索还是生成笔记？"
        assert capture == []                       # ReAct 未启动，LLM 未调用
        assert agent.last_intent.clarification == "要搜索还是生成笔记？"

    def test_clarification_force_dispatch_proceeds(self):
        capture = []
        pipeline = MockIntentPipeline(result=_intent(IntentType.ASK_QUESTION,
                                                     clarification="要哪个？"))
        session = Session()
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test", intent_enabled=True,
                      intent_pipeline=pipeline, session=session)
        text = asyncio.run(agent.run("整理这篇", force_dispatch=True))
        assert text == "Done."                     # 超轮终止：ReAct 正常跑
        assert len(capture) == 1
        assert any("INTENT:" in m.content for m in capture[0])
        # M5：INTENT 块必须排除 clarification / prev_intent——澄清只走 CLI 层
        # （避免与 AskUserTool 双问）；prev_intent 是 session 内部状态，不暴露给
        # Supervisor。force_dispatch 用例（澄清文本存在但被 force 跳过）正好可断言。
        intent_msg = next(m for m in capture[0] if "INTENT:" in m.content)
        assert "要哪个？" not in intent_msg.content     # clarification 被排除
        assert "prev_intent" not in intent_msg.content  # prev_intent 被排除

    def test_pipeline_failure_degrades(self):
        capture = []
        pipeline = MockIntentPipeline(exc=RuntimeError("Stage 3 网络超时"))
        session = Session()
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test", intent_enabled=True,
                      intent_pipeline=pipeline, session=session)
        text = asyncio.run(agent.run("搜索 x"))
        assert text == "Done."                     # 降级：普通 ReAct 继续，不崩
        assert agent.last_intent is None
        assert not any("INTENT:" in m.content for m in capture[0])

    def test_session_updated_after_run(self):
        capture = []
        pipeline = MockIntentPipeline(result=_intent(IntentType.SEARCH_PAPER))
        session = Session()
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test", intent_enabled=True,
                      intent_pipeline=pipeline, session=session)
        asyncio.run(agent.run("搜索 circRNA"))
        assert session.prev_intent == IntentType.SEARCH_PAPER
        assert session.prev_user_input == "搜索 circRNA"

    def test_pipeline_failure_does_not_update_session(self):
        capture = []
        pipeline = MockIntentPipeline(exc=RuntimeError("boom"))
        session = Session()
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test", intent_enabled=True,
                      intent_pipeline=pipeline, session=session)
        asyncio.run(agent.run("搜索 x"))
        assert session.prev_intent is None          # 降级轮不更新 prev_intent

    def test_task_surrogates_sanitized_before_session(self):
        """2026-08-05 回归：run() 入口清洗未配对 surrogate（信任边界）——否则
        session.prev_user_input 携带脏字符，下轮意图管线从它重提实体时再注入。
        正常输入零开销（sanitize_surrogates 无匹配返回原串）。"""
        capture = []
        pipeline = MockIntentPipeline(result=_intent(IntentType.GENERATE_NOTE))
        session = Session()
        llm = make_capture_llm([Message(role="assistant", content="Done.")], capture)
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]),
                      agent_type="test", intent_enabled=True,
                      intent_pipeline=pipeline, session=session)
        asyncio.run(agent.run("笔记\udce5脏字符"))
        assert "\udce5" not in session.prev_user_input


# ─── 流式输出（Task 3）：StreamEvent + stream_callback 门控 ──────────


class TestStreaming:
    @pytest.mark.asyncio
    async def test_no_stream_callback_uses_chat(self):
        """门控回归（F1）：mock LLM 只有 chat（无 chat_stream），stream_callback=None
        → run 走 chat 路径；若误调 chat_stream 会因 MagicMock 不可 await 抛 TypeError。"""
        llm = make_mock_llm([Message(role="assistant", content="ok")])
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test")
        assert await agent.run("hi") == "ok"

    @pytest.mark.asyncio
    async def test_streams_content_deltas_via_callback(self):
        events = []
        llm = MagicMock()

        async def chat_stream(messages, tools=None, tool_choice="auto", on_delta=None):
            on_delta("你好")
            on_delta("世界")
            return Message(role="assistant", content="你好世界")

        llm.chat_stream = chat_stream
        llm.model = "mock"
        agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test",
                      stream_callback=events.append)
        assert await agent.run("hi") == "你好世界"
        assert [e.kind for e in events] == ["content", "content"]
        assert [e.text for e in events] == ["你好", "世界"]
        assert all(e.agent_type == "test" for e in events)


# ─── 工具状态行（Task 4）：_format_tool_call + _exec_tool 工具事件 ──


class TestFormatToolCall:
    def test_shows_compacted_args(self):
        from paperflow.core.agent import _format_tool_call
        assert _format_tool_call(
            "search_paper", '{"query": "circRNA", "max_results": 5}'
        ) == "调用 search_paper(query=circRNA, max_results=5)"

    def test_falls_back_on_invalid_json(self):
        from paperflow.core.agent import _format_tool_call
        assert _format_tool_call("echo", "{bad json") == "调用 echo"

    def test_empty_or_missing_args_just_name(self):
        from paperflow.core.agent import _format_tool_call
        assert _format_tool_call("echo", "{}") == "调用 echo"
        assert _format_tool_call("echo", "") == "调用 echo"

    def test_none_args_tolerated(self):
        """回归（final review）：provider 返回 arguments=None 时不得 AttributeError
        ——raw_args.strip() 前须 (raw_args or "") 兜底，退化显示工具名。"""
        from paperflow.core.agent import _format_tool_call
        assert _format_tool_call("echo", None) == "调用 echo"

    def test_args_truncated_to_line_budget(self):
        """回归（final review）：“整行 ≤80”须按“固定前缀后的剩余预算”截断 pairs——
        工具名过长时若仍 pairs[:80] 会整行超 80。工具名自身超宽时退化为纯工具名。"""
        from paperflow.core.agent import _format_tool_call
        long_name = "spawn_sub_agent_with_quite_a_long_name_here_ok"
        args = ('{"query": "' + "x" * 80 + '", "max_results": 5, '
                '"sort": "relevance", "year": "2024", "extra": 1}')
        line = _format_tool_call(long_name, args)
        assert len(line) <= 80
        assert line.startswith(f"调用 {long_name}(")
        # 工具名长到剩余预算 ≤0 → 只显示工具名（宁可超宽也不截断名字）
        mega = "tool_" + "x" * 100
        assert _format_tool_call(mega, args).startswith("调用")

    def test_path_arg_shown_in_full(self):
        """2026-08-05 回归：绝对路径参数完整展示不截断——真实冒烟反馈路径被截成
        .../Obsidian V... 看不出在读哪个文件。含路径的行也不压 80（终端可换行）。"""
        from paperflow.core.agent import _format_tool_call
        path = ("/Users/gnomeshgh/Documents/Obsidian Vault/paper/pdf/"
                "link prediction/circRNA-disease/GMNN2CD.pdf")
        line = _format_tool_call("read_pdf", f'{{"path": "{path}"}}')
        assert path in line            # 完整路径在行内
        assert "..." not in line       # 未被截断
        assert line.startswith("调用 read_pdf(path=")


class TestToolEvent:
    @pytest.mark.asyncio
    async def test_emits_tool_event_before_execution(self):
        from tests.conftest import MockEchoTool

        events = []
        responses = [
            Message(role="assistant", content=None, tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "echo", "arguments": '{"message": "hi"}'}}]),
            Message(role="assistant", content="done"),
        ]
        llm = MagicMock()

        async def chat_stream(messages, tools=None, tool_choice="auto", on_delta=None):
            return responses.pop(0)

        llm.chat_stream = chat_stream
        llm.model = "mock"
        agent = Agent(llm=llm, agent_registry=make_mock_registry([MockEchoTool()]),
                      agent_type="test", stream_callback=events.append)
        assert await agent.run("hi") == "done"
        tool_events = [e for e in events if e.kind == "tool"]
        assert len(tool_events) == 1
        assert tool_events[0].text == "调用 echo(message=hi)"
        assert tool_events[0].agent_type == "test"


# ─── 截断续写（generate-note-fix Task 3）：半截回答不当作最终结果 ─────


def test_truncated_response_continues_and_merges():
    capture = []
    # 第一个响应截断(truncated=True)→ 触发续写；第二个完整 → 合并返回
    llm = make_capture_llm([
        Message(role="assistant", content="前半", truncated=True),
        Message(role="assistant", content="后半"),
    ], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test")
    result = asyncio.run(agent.run("问题"))
    assert result == "前半后半"          # 半截+续写合并
    assert len(capture) == 2             # LLM 被调用两次（截断→续写）
    # 续写请求携带"继续"提示
    cont = [m for m in capture[1] if m.role == "user"]
    assert any("截断" in (m.content or "") for m in cont)


def test_non_truncated_returns_directly():
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="回答")], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test")
    result = asyncio.run(agent.run("问题"))
    assert result == "回答"
    assert len(capture) == 1


# ─── B1 并发 tool_calls（Task 9）：同一 message 的多个 tool_call 并发执行 ──


def test_multiple_tool_calls_executed_in_parallel():
    """B1：同一 message 的两个 tool_call 并发执行。

    用墙钟区分：串行 = 2×0.05s sleep ≈ 0.10s+；并行 = max ≈ 0.05s。
    给 0.08s 余量（0.05+两轮调度开销 < 0.08 < 0.10+）。"""
    import json
    import time
    from pathlib import Path

    from paperflow.core.agent import Agent
    from paperflow.core.agent_registry import AgentRegistry
    from paperflow.core.llm import Message
    from paperflow.core.tool import Tool, ToolResult
    from tests.conftest import make_mock_llm

    class SlowEchoTool(Tool):
        name = "slow_echo"
        description = "sleep 后回显"
        parameters = {"type": "object",
                      "properties": {"msg": {"type": "string"}},
                      "required": ["msg"]}

        def execute(self, msg):
            time.sleep(0.05)
            return ToolResult(text=f"Echo: {msg}")

    # 绝对路径定位 agents/ 目录（tests/ 的父级），与 conftest.py 的 agents_dir
    # 解析方式一致，避免依赖 pytest 的 CWD。demo agent 目录名 = _demo。
    reg = AgentRegistry(str(Path(__file__).resolve().parents[1] / "agents"))
    agent = Agent(llm=make_mock_llm([]), agent_registry=reg, agent_type="_demo")
    agent.tools = {"slow_echo": SlowEchoTool()}   # mock LLM 直接驱动 tool_call，schema 无关

    calls = [Message(role="assistant", content=None, tool_calls=[
        {"id": "c1", "type": "function",
         "function": {"name": "slow_echo", "arguments": json.dumps({"msg": "a"})}},
        {"id": "c2", "type": "function",
         "function": {"name": "slow_echo", "arguments": json.dumps({"msg": "b"})}},
    ])]
    # ReAct 两轮：轮1 返回双 tool_call → Agent 并发执行并把两条 tool 结果附进对话；
    # 轮2 返回最终回答。tool 结果由 Agent 自己 append，mock 只需提供两轮 LLM 响应。
    resp = [calls[0], Message(role="assistant", content="done")]
    agent.llm = make_mock_llm(resp)

    t0 = time.monotonic()
    out = asyncio.run(agent.run("go"))
    dt = time.monotonic() - t0
    assert out == "done"
    assert dt < 0.08, f"tool_calls 疑似串行执行: {dt:.3f}s（应 ≈0.05s）"
