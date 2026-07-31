"""python -m paperflow — Layer 0 demo: ReAct loop with _demo agent."""
import asyncio

from paperflow.config import PaperFlowConfig
from paperflow.core.llm import LLMClient
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.agent import Agent


async def main() -> None:
    config = PaperFlowConfig.from_env()
    llm = LLMClient(config.llm)
    registry = AgentRegistry(config.agents_dir)
    agent = Agent(llm=llm, agent_registry=registry, agent_type="_demo")
    result = await agent.run("Hello, echo this message!")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
