"""python -m paperflow — Layer 1: security middleware wired."""
import asyncio

from paperflow.config import PaperFlowConfig
from paperflow.core.llm import LLMClient
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.agent import Agent
from paperflow.core.security import (
    AuditMiddleware,
    WorkspacePolicyMiddleware,
    SecurityScanMiddleware,
    PolicyEngineMiddleware,
)


async def main() -> None:
    config = PaperFlowConfig.from_env()
    llm = LLMClient(config.llm)
    registry = AgentRegistry(config.agents_dir)

    middlewares = [
        AuditMiddleware(),
        WorkspacePolicyMiddleware(workspace=config.workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk=config.max_risk),
    ]

    agent = Agent(
        llm=llm,
        agent_registry=registry,
        agent_type="_demo",
        security_middleware=middlewares,
    )
    result = await agent.run("Hello, echo this message!")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
