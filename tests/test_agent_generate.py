"""generate-note agent + ReviewDraftTool 测试。"""
import asyncio
from pathlib import Path

import pytest

from paperflow.core.llm import Message
from tests.conftest import make_mock_llm, make_agent


@pytest.mark.asyncio
async def test_review_draft_runs_child_and_cleans_scratch(agent_env, agent_registry):
    cfg, _ = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    # mock LLM：子 review-note 首轮即回最终意见（不调工具）——最简验证桥接与清理
    llm = make_mock_llm([
        Message(role="assistant", content="审稿意见：结构完整"),
    ])
    agent = make_agent(agent_registry, "generate-note", llm, cfg)
    tool = agent.tools["review_draft"]
    assert tool.needs_parent is True

    # 模拟生产：execute 经 asyncio.to_thread 跑在 worker 线程（无 running loop）
    result = await asyncio.to_thread(
        tool.execute,
        draft_text="# 标题\n## 概述\n## 方法\n",
        pdf_path=str(pdf),
    )
    assert "审稿意见" in result.text          # 子 agent 跑过（文本来自 mock 的 child 轮）
    # scratch 清理：workspace/tmp 下无残留
    scratch = Path(cfg.workspace) / "tmp"
    assert not list(scratch.glob("review_*.md"))
