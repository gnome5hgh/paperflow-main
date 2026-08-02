"""review-note 审稿 agent 测试：mock LLM 驱动 ReAct，工具经真实安全链执行。"""
from pathlib import Path

import pytest

from paperflow.core.llm import Message
from tests.conftest import make_mock_llm, _tc, make_agent


@pytest.mark.asyncio
async def test_review_note_happy_path(agent_env, agent_registry):
    cfg, _ = agent_env
    # 草稿（scratch 位置，任务文本给出）+ 原文 PDF（假解析器不读内容，文件需存在）
    scratch = Path(cfg.workspace) / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    draft = scratch / "review_test.md"
    draft.write_text("# 标题\n## 概述\n## 方法\n", encoding="utf-8")
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")

    llm = make_mock_llm([
        _tc("read_file", {"path": str(draft)}),
        _tc("read_pdf", {"path": str(pdf)}),
        _tc("format_check", {"path": str(draft)}),
        _tc("suggest_edit", {"path": str(draft), "suggestions": ["补充实验结果"]}),
        Message(role="assistant", content="审稿完成：建议补充实验结果"),
    ])
    agent = make_agent(agent_registry, "review-note", llm, cfg)
    result = await agent.run(f"审阅草稿文件 {draft}，对照原文 {pdf}")
    assert "审稿完成" in result
