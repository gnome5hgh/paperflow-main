"""answer-question 问答 agent 测试：三 mode + RAG 空降级。"""
from pathlib import Path

import pytest

from paperflow.core.llm import Message
from tests.conftest import make_mock_llm, _tc, make_agent


@pytest.mark.asyncio
async def test_answer_question_read_pdf_mode(agent_env, agent_registry):
    cfg, _ = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    llm = make_mock_llm([
        _tc("read_pdf", {"path": str(pdf)}),
        _tc("mark_read", {"path": str(pdf)}),
        _tc("format_answer", {"answer": "该论文介绍..."}),
        Message(role="assistant", content="回答完成"),
    ])
    agent = make_agent(agent_registry, "answer-question", llm, cfg)
    result = await agent.run(f"这篇论文讲了什么？ {pdf}")
    assert "回答完成" in result
    # mark_read 落 tmp memory history（隔离；不落真实 vault）
    history = Path(cfg.workspace) / "memory" / "history.jsonl"
    assert history.exists()


@pytest.mark.asyncio
async def test_answer_question_rag_empty_degrades(agent_env, agent_registry):
    cfg, _ = agent_env
    # svc 用 FakeEmbedder 但空索引 → RagRetrieveTool 返回"检索无命中"→ LLM 如实降级
    llm = make_mock_llm([
        _tc("rag_retrieve", {"query": "circRNA 机制", "top_k": 3}),
        _tc("format_answer", {"answer": "知识库暂未检索到相关内容"}),
        Message(role="assistant", content="已回答"),
    ])
    agent = make_agent(agent_registry, "answer-question", llm, cfg)
    result = await agent.run("circRNA 的调控机制是什么？")
    assert "已回答" in result


@pytest.mark.asyncio
async def test_answer_question_notes_mode(agent_env, agent_registry):
    cfg, _ = agent_env
    note = Path(cfg.vault_note_dir) / "old.md"
    note.write_text("# 旧笔记\n\n内容", encoding="utf-8")
    llm = make_mock_llm([
        _tc("read_file", {"path": str(note)}),
        _tc("format_answer", {"answer": "笔记内容..."}),
        Message(role="assistant", content="已回答"),
    ])
    agent = make_agent(agent_registry, "answer-question", llm, cfg)
    result = await agent.run(f"我之前的笔记 {note} 写了什么？")
    assert "已回答" in result
