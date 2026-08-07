# scripts/verify_agents.py
"""真实 LLM smoke：4 个业务 agent 的 happy path（代码资产，验收后手动执行）。

用法：
    conda run -n paperflow python scripts/verify_agents.py <pdf绝对路径> [草稿绝对路径]
需 PAPERFLOW_API_KEY（DeepSeek/OpenAI 兼容，经 config）。
路径必须为绝对路径：WorkspacePolicy 按绝对路径白名单门控，相对路径会被拒绝。

副作用（如实声明）：
- search-paper 会真打 arXiv/OpenAlex 公开 API
- answer-question 的 RAG 检索会加载真实 bge 模型（首次下载权重 ~30MB）
- generate-note 会真写一篇笔记到配置的 vault note 目录（供检查）
- reviewer 读草稿（argv[2]，可复用 generate-note 写出的笔记）对照原文（argv[1]）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # scripts/ 不在包内

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.llm import LLMClient
from paperflow.core.security import (
    AuditMiddleware, WorkspacePolicyMiddleware,
    SecurityScanMiddleware, PolicyEngineMiddleware,
)


async def _accept_confirm(cr):
    """smoke 自动接受确认：验 happy path 主链路。

    必须提供——generate-note 定稿的 WriteFileTool requires_confirm=True
    （spec §4.1 定稿是用户门），无此回调则 "User denied: write_file"，
    "真写笔记"副作用实际不会发生（与脚本 docstring 声明矛盾）。"""
    return True


def _make_agent(registry, agent_type, config):
    """与 __main__.py 一致的中间件栈（真实安全链路）+ 自动确认。"""
    middlewares = [
        AuditMiddleware(),
        WorkspacePolicyMiddleware(workspace=config.workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk=config.max_risk),
    ]
    return Agent(llm=LLMClient(config.llm), agent_registry=registry,
                 agent_type=agent_type, security_middleware=middlewares,
                 confirm_callback=_accept_confirm)


async def main(pdf: str, draft: str | None) -> int:
    config = PaperFlowConfig.from_env()
    registry = AgentRegistry(config.agents_dir)

    print("== 1. search-paper（真实 arXiv/OpenAlex）==")
    print(await _make_agent(registry, "search-paper", config).run(
        "搜索 2023 年异构图神经网络链路预测的论文，最多 3 篇"))

    print("== 2. answer-question（RAG 检索，首次加载 bge 模型）==")
    print(await _make_agent(registry, "answer-question", config).run(
        "circRNA 的调控机制是什么？"))

    print("== 3. generate-note（真写笔记到 vault note/）==")
    print(await _make_agent(registry, "generate-note", config).run(
        f"为 {pdf} 生成笔记"))

    print("== 4. reviewer（审稿 generate-note 产出的笔记）==")
    if draft:
        print(await _make_agent(registry, "reviewer", config).run(
            f"审阅草稿文件 {draft}，对照原文 {pdf}"))
    else:
        print("跳过：未提供草稿路径（argv[2]）")
    return 0


if __name__ == "__main__":
    pdf = sys.argv[1] if len(sys.argv) > 1 else input("PDF 绝对路径: ")
    draft = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(asyncio.run(main(pdf, draft)))
