# paperflow/core/agent.py
"""
Agent 基类 —— ReAct（Reasoning + Acting）循环的核心实现。

Supervisor 和所有 SubAgent 使用同一个 Agent 类，差异仅在于
构造函数传入的 ``agent_type`` 不同 —— Agent 通过 AgentRegistry
按类型加载对应的 system_prompt 和 Tool 集合。

设计依据：

- **ADR 0003**：权限最小化 —— Supervisor 只加载 supervisor 组的调度类 Tool，
  SubAgent 只加载领域类 Tool，互不越界
- **ReAct 循环**：Thought → Act → Obs → ... → Finish，
  LLM 自主决定何时停止（返回无 tool_calls 的 content 时）
- **Pull 模式**：Agent 不接收外部组装的 tools 列表，而是通过 agent_type
  从 AgentRegistry 拉取配置，保证 Tool 权限的集中控制

ReAct 循环流程::

    1. 构建初始 messages = [system_prompt, user_task]
    2. LLM 调用 → response
    3. 如果 response 无 tool_calls → 返回 response.content（结束）
    4. 如果 response 有 tool_calls → 执行每个 tool_call
    5. 将 tool 结果附加到 messages → 回到步骤 2
    6. 超过 max_turns → 抛出 MaxTurnsExceeded

错误处理策略：

- **_exec_tool 中的异常被内部捕获**：JSON 解析失败、未知工具名、
  工具执行异常都转为 ToolResult(text="...")，作为正常对话流的一部分
  反馈给 LLM，由 LLM 自行决定是否重试或调整参数
- **only MaxTurnsExceeded 向上抛**：这是唯一"不可恢复"的错误 ——
  LLM 陷入了无法自主退出的循环，需要调用方介入
"""

import json

from paperflow.core.llm import LLMClient, Message, tool_to_openai_schema
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.tool import ToolResult


class MaxTurnsExceeded(Exception):
    """
    ReAct 循环在 max_turns 轮内未产生最终回答时抛出。

    这是 Agent 内置的安全阀 —— 防止 LLM 陷入无限 tool-calling 循环
    （例如 LLM 反复调用同一个工具但不用其结果给出最终回答）。
    调用方（Supervisor 或 CLI）捕获此异常后应终止任务并向用户报告。
    """


class Agent:
    """
    ReAct 循环的执行单元，Supervisor 和 SubAgent 共用。

    构造方式（pull 模式）::

        agent = Agent(
            llm=llm_client,
            agent_registry=registry,
            agent_type="search-paper",
        )
        result = await agent.run("搜索异构图神经网络的最新论文")

    Agent 通过 ``agent_type`` 从 ``AgentRegistry`` 拉取：
    - system_prompt：注入 LLM 的行为规范
    - tools：本 Agent 可调用的 Tool 集合
    - allowed_spawns：本 Agent 能 spawn 哪些 SubAgent（Layer 4 使用）
    """

    def __init__(
        self,
        llm: LLMClient,
        agent_registry: AgentRegistry,
        agent_type: str,
        max_turns: int = 20,
    ):
        """
        :param llm: LLM 客户端实例
        :param agent_registry: Agent 注册表，从中按 agent_type 拉取配置
        :param agent_type: Agent 类型标识符（对应 agents/<agent_type>/ 目录）
        :param max_turns: ReAct 循环最大轮数，防止死循环
        """
        # Pull 模式：从唯一注册表按类型加载完整配置
        config = agent_registry.get_config(agent_type)

        #: LLM 客户端（async 接口）
        self.llm = llm

        #: Tool 字典，key = tool.name，供 _exec_tool 快速查找
        self.tools = {t.name: t for t in config.tools}

        #: 注入 LLM 的系统提示词，定义本 Agent 的行为规范
        self.system_prompt = config.system_prompt

        #: Agent 类型标识符
        self.agent_type = agent_type

        #: ReAct 循环最大轮数安全阀
        self.max_turns = max_turns

        #: 预计算的 OpenAI function calling JSON Schema 列表
        #: 在构造时转换一次，避免每轮 run 都重复转换
        self._tool_schemas = [tool_to_openai_schema(t) for t in config.tools]

    async def run(self, task: str) -> str:
        """
        执行 ReAct 循环，返回 LLM 的最终文本回答。

        这是 Agent 的唯一公共入口。调用方（CLI、Supervisor 的 SpawnSubAgentTool）
        只需要传入任务文本，等待返回结果。

        :param task: 用户任务文本（对于 Supervisor 是原始用户输入；
                     对于 SubAgent 是 Supervisor 拆分后的子任务）
        :returns: LLM 的最终文本回答
        :raises MaxTurnsExceeded: 超过 max_turns 轮仍未停止

        ReAct 循环步骤::

            1. 构建初始消息列表 [system_prompt, user_task]
            2. 调用 LLM → 获取 response
            3. 如果无 tool_calls → 返回 content（LLM 判定任务完成）
            4. 如果有 tool_calls → 逐个执行，将 ToolResult 附加到消息列表
            5. 回到步骤 2，LLM 根据工具执行结果继续推理
            6. 若超过 max_turns → 抛出 MaxTurnsExceeded（安全阀）
        """
        # 构建初始对话上下文
        messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=task),
        ]

        for _ in range(self.max_turns):
            # 调用 LLM，传入当前对话历史和可用工具 Schema
            response = await self.llm.chat(
                messages,
                tools=self._tool_schemas if self._tool_schemas else None,
            )

            # LLM 判定任务完成：返回无 tool_calls 的纯文本消息
            if not response.tool_calls:
                return response.content

            # LLM 请求调用工具：将 assistant 消息（含 tool_calls）加入对话
            messages.append(response)

            # 逐个执行 LLM 请求的工具调用
            for tc in response.tool_calls:
                result = self._exec_tool(tc)

                # 将工具执行结果以 tool 角色消息加入对话
                # tool_call_id 将这条结果关联到 LLM 请求的对应 tool_call
                messages.append(Message(
                    role="tool",
                    content=result.text,
                    tool_call_id=tc["id"],
                ))

        # 安全阀触发：LLM 陷入了无法在限定轮数内退出的循环
        raise MaxTurnsExceeded(
            f"ReAct loop did not finish within {self.max_turns} turns"
        )

    def _exec_tool(self, tool_call: dict) -> ToolResult:
        """
        执行单个 LLM 请求的工具调用，内部处理所有异常。

        错误处理采用"degrade to text"策略：
        所有异常（JSON 解析失败、未知工具名、工具执行异常）
        都转为 ToolResult(text="...")，作为正常对话流的一部分
        反馈给 LLM。LLM 在下一轮 ReAct 中看到错误文本后
        可以自行决定是否重试、调整参数或放弃。

        :param tool_call: LLM 返回的工具调用字典
            {"id": str, "function": {"name": str, "arguments": str}}
            其中 arguments 为 JSON 字符串，此方法负责 json.loads 解析
        :returns: ToolResult，始终返回（不抛异常）
        """
        name = tool_call["function"]["name"]

        # 1. 解析 LLM 生成的 JSON 参数字符串
        try:
            args = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError as e:
            # LLM 生成了非法 JSON → 反馈给 LLM，让它重新生成
            return ToolResult(text=f"Tool argument parse error: {e}")

        # 2. 按工具名查找 Tool 实例
        tool = self.tools.get(name)
        if tool is None:
            # LLM 请求了不存在的工具（幻觉或 prompt injection）→ 反馈可用工具列表
            return ToolResult(
                text=f"Unknown tool: {name}. Available: {list(self.tools.keys())}"
            )

        # 3. 执行工具逻辑
        try:
            return tool.execute(**args)
        except Exception as e:
            # 工具执行失败（网络超时、文件不存在等）→ 反馈给 LLM
            return ToolResult(text=f"Tool error: {e}")
