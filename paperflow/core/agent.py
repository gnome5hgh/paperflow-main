# paperflow/core/agent.py
import json

from paperflow.core.llm import LLMClient, Message, tool_to_openai_schema
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.tool import ToolResult


class MaxTurnsExceeded(Exception):
    """ReAct loop did not finish within max_turns."""


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        agent_registry: AgentRegistry,
        agent_type: str,
        max_turns: int = 20,
    ):
        config = agent_registry.get_config(agent_type)
        self.llm = llm
        self.tools = {t.name: t for t in config.tools}
        self.system_prompt = config.system_prompt
        self.agent_type = agent_type
        self.max_turns = max_turns
        self._tool_schemas = [tool_to_openai_schema(t) for t in config.tools]

    async def run(self, task: str) -> str:
        messages = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=task),
        ]

        for _ in range(self.max_turns):
            response = await self.llm.chat(
                messages,
                tools=self._tool_schemas if self._tool_schemas else None,
            )

            if not response.tool_calls:
                return response.content

            messages.append(response)
            for tc in response.tool_calls:
                result = self._exec_tool(tc)
                messages.append(Message(
                    role="tool",
                    content=result.text,
                    tool_call_id=tc["id"],
                ))

        raise MaxTurnsExceeded(
            f"ReAct loop did not finish within {self.max_turns} turns"
        )

    def _exec_tool(self, tool_call: dict) -> ToolResult:
        name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError as e:
            return ToolResult(text=f"Tool argument parse error: {e}")
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(
                text=f"Unknown tool: {name}. Available: {list(self.tools.keys())}"
            )
        try:
            return tool.execute(**args)
        except Exception as e:
            return ToolResult(text=f"Tool error: {e}")
