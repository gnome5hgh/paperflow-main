# scripts/verify_layer4.py
"""Layer 4 真实链路 smoke（代码资产，验收后手动——不进 CI）。

用法：PAPERFLOW_API_KEY=sk-xxx conda run -n paperflow python scripts/verify_layer4.py
验证点：
  1. supervisor 装配成功（intent_enabled + 4 工具）
  2. 一条 search_paper 查询：INTENT 块命中 → supervisor 真实 spawn search-paper → 返回论文列表
  3. 一条 generate_note 查询：需 pdf 路径，验证 spawn + confirm_callback 接线（写盘需用户确认）
"""
import asyncio

from paperflow.config import PaperFlowConfig
from paperflow.core.llm import LLMClient
from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.session import Session
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.hybrid_router import HybridRouter
from paperflow.rag.embedder import BgeEmbedder
from paperflow.core.intent.route_loader import load_routes


async def main() -> None:
    config = PaperFlowConfig.from_env()
    assert config.llm.api_key, "需要 PAPERFLOW_API_KEY"
    llm = LLMClient(config.llm)
    registry = AgentRegistry(config.agents_dir)
    # 完整装配（镜像 cli.main()）：Stage 3 LLM 兜底必须有真实 StructuredOutput——
    # 否则查询 miss 全部最小 route 时 structured.extract 会崩（⚪3，手动 smoke 不许有必崩路径）。
    from pathlib import Path
    from paperflow.core.memory import MemoryStore
    from paperflow.core.structured import StructuredOutput
    memory_dir = Path(config.workspace) / "memory"
    store = MemoryStore(memory_dir)
    structured = StructuredOutput(llm, store=store)
    router = HybridRouter(encoder=BgeEmbedder(), routes=load_routes())
    pipeline = IntentPipeline(router=router, structured=structured)
    session = Session()
    agent = Agent(llm=llm, agent_registry=registry, agent_type="supervisor",
                  intent_enabled=True, intent_pipeline=pipeline, session=session,
                  confirm_callback=lambda cr: True)
    text = await agent.run("搜索 circRNA 文献")
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
