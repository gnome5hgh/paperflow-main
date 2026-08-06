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
        Message(role="assistant", content="已回答"),
    ])
    agent = make_agent(agent_registry, "answer-question", llm, cfg)
    result = await agent.run(f"我之前的笔记 {note} 写了什么？")
    assert "已回答" in result


def test_answer_question_excludes_format_answer(agent_env, agent_registry):
    """回归（真实 CLI 冒烟）：answer-question 不再装配 format_answer。

    冒烟发现该 agent 用 format_answer 格式化最终回答会劣化质量——常把"已读取"
    这类状态文本当 answer 传入，产出无用的 `## 回答` 包装，真实答案另起炉灶。
    内容安全由 on_finish 兜底，移除无安全缺口。"""
    cfg, _ = agent_env
    agent = make_agent(agent_registry, "answer-question", make_mock_llm([]), cfg)
    assert "format_answer" not in agent.tools


def test_answer_question_has_glob_grep(agent_registry):
    """Task 4：answer-question 装配 glob/grep——按模式定位笔记/论文再读。

    三 mode（读 PDF/查笔记/RAG）都涉及按名称定位文件（论文 PDF、旧笔记），
    不再盲猜精确路径（P2 路径风暴根因）：glob 枚举 + grep 核对内容锚点。
    只读工具 risk=low，无确认门——装配名单断言防回归。"""
    config = agent_registry.get_config("answer-question")
    names = {t.name for t in config.tools}
    assert {"glob", "grep"} <= names
