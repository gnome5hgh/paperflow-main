# scripts/verify_layer4.py
"""Layer 4 真实链路 smoke（代码资产，验收后手动——不进 CI）。

用法：PAPERFLOW_API_KEY=sk-xxx conda run -n paperflow python scripts/verify_layer4.py
验证点：
  1. supervisor 装配成功（intent_enabled + 4 工具）
  2. 一条 search_paper 查询：INTENT 块命中 → supervisor 真实 spawn searcher → 返回论文列表
  3. generate_note + confirm 接线走 `python -m paperflow` 交互式验证，本脚本只 smoke search_paper
"""
import asyncio

from paperflow.config import PaperFlowConfig
from paperflow.core.llm import LLMClient
from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.session import Session
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.hybrid_router import HybridRouter
from paperflow.rag.embedder import BgeEmbedder, resolve_model_dir
from paperflow.core.intent.route_loader import load_routes


async def _auto_confirm(cr) -> bool:
    """程序化自动确认回调。

    必须 async：Agent._exec_tool 以 `await self.confirm_callback(cr)` 调用
    （agent.py:411）——sync lambda 返回 True 在 ConfirmRequired 触发时会抛
    `TypeError: object bool can't be used in 'await' expression`（审阅 R1）。
    """
    return True


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
    # 模型路径本地优先（data/models/<name>），回退 HF 名（resolve_model_dir）
    router = HybridRouter(
        encoder=BgeEmbedder(model_name=resolve_model_dir(config.workspace, config.embed_model)),
        routes=load_routes())
    pipeline = IntentPipeline(router=router, structured=structured)
    session = Session()
    agent = Agent(llm=llm, agent_registry=registry, agent_type="supervisor",
                  intent_enabled=True, intent_pipeline=pipeline, session=session,
                  confirm_callback=_auto_confirm)
    text = await agent.run("搜索 circRNA 文献")
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
