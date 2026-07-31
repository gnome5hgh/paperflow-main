"""python -m paperflow — Layer 1: memory + security wired."""
import asyncio
from pathlib import Path

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
from paperflow.core.structured import StructuredOutput
from paperflow.core.memory import (
    MemoryStore, ExperienceMemoryMiddleware, MemoryIndex,
    ContextCompressor, GitStore, Dream,
)


async def main() -> None:
    config = PaperFlowConfig.from_env()
    llm = LLMClient(config.llm)
    registry = AgentRegistry(config.agents_dir)

    memory_dir = Path(config.workspace) / "memory"
    store = MemoryStore(memory_dir)
    git = GitStore(memory_dir)
    structured = StructuredOutput(llm, store=store)

    middlewares = [
        AuditMiddleware(),
        WorkspacePolicyMiddleware(workspace=config.workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk=config.max_risk),
        ExperienceMemoryMiddleware(store),
    ]

    agent = Agent(
        llm=llm,
        agent_registry=registry,
        agent_type="_demo",
        security_middleware=middlewares,
        memory_index=MemoryIndex(memory_dir),
        compressor=ContextCompressor(config.context, llm, structured=structured),
    )
    result = await agent.run("Hello, echo this message!")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
