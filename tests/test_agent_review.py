"""review-note 审稿 agent 测试：mock LLM 驱动 ReAct，工具经真实安全链执行。"""
from pathlib import Path

import pytest

from paperflow.core.llm import Message
from paperflow.tools.factory import make_tools
from paperflow.tools import SuggestEditTool
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


def test_suggest_edit_allows_scratch(agent_env):
    """确定性校验：SuggestEditTool 经 make_tools 装配后，scratch 绝对路径在 allowed_paths 内。

    防回归：审稿流目标的是 scratch 草稿路径，若 allowed_roots 退回 ["note"]，
    happy-path 测试仍会通过（mock LLM 不读工具结果），但真实 WorkspacePolicy 会
    拦截 suggest_edit —— 此断言把"放开 scratch 根"变成可验证的事实。"""
    cfg, _ = agent_env
    tool = make_tools(cfg, [SuggestEditTool])[0]
    assert str(Path(cfg.workspace) / "tmp") in tool.allowed_paths
