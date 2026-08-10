"""reviewer 笔记审查模式 agent 测试：mock LLM 驱动 ReAct，工具经真实安全链执行。

review-note 改造为 reviewer 后（Task 6），原 review-note 的笔记审查测试保留在
本文件，agent_type 全部改指 "reviewer"（任务前缀「审阅草稿文件…」触发笔记审查模式）。
"""
from pathlib import Path

import pytest

from paperflow.core.llm import Message
from paperflow.tools.common.factory import make_tools
from paperflow.tools import SubmitReviewTool
from tests.conftest import make_mock_llm, _tc, make_agent


@pytest.mark.asyncio
async def test_reviewer_happy_path(agent_env, agent_registry):
    cfg, _ = agent_env
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
        _tc("submit_review", {"path": str(draft), "verdict": "pass", "issues": []}),
        Message(role="assistant", content="审查裁决：pass"),
    ])
    agent = make_agent(agent_registry, "reviewer", llm, cfg)
    result = await agent.run(f"审阅草稿文件 {draft}，对照原文 {pdf}")
    assert "审查裁决" in result


def test_submit_review_allows_scratch(agent_env):
    """确定性校验：SubmitReviewTool 经 make_tools 装配后，scratch 绝对路径在 allowed_paths 内。

    防回归：审稿流目标的是 scratch 草稿路径，若 allowed_roots 退回 ["note"]，
    happy-path 测试仍会通过（mock LLM 不读工具结果），但真实 WorkspacePolicy 会
    拦截 submit_review —— 此断言把"放开 scratch 根"变成可验证的事实。"""
    cfg, _ = agent_env
    tool = make_tools(cfg, [SubmitReviewTool])[0]
    assert str(Path(cfg.workspace) / "tmp") in tool.allowed_paths


def test_reviewer_has_glob_grep(agent_registry):
    """Task 4：reviewer 装配 glob/grep——事实核对时在 vault 内搜索对照。

    reviewer 审稿要核对草稿断言与原文/其他笔记是否一致（grep 搜文本锚点）、
    定位相关文件（glob）；不再依赖"路径由任务文本给出"的单一通道（P2 路径风暴
    根因的审稿侧治理）。只读工具 risk=low，无确认门——名单断言防回归。"""
    config = agent_registry.get_config("reviewer")
    names = {t.name for t in config.tools}
    assert {"glob", "grep"} <= names


@pytest.mark.asyncio
async def test_reviewer_submits_fail_verdict(agent_env, agent_registry):
    """fail 路径：submit_review 带 blocking issue（结构缺章节）。"""
    cfg, _ = agent_env
    scratch = Path(cfg.workspace) / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    draft = scratch / "review_test.md"
    draft.write_text("# 标题\n## 概述\n## 方法\n", encoding="utf-8")
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    issues = [{"severity": "blocking", "dimension": "structure",
               "location": "实验结果", "action": "补充实验结果章节"}]
    llm = make_mock_llm([
        _tc("read_file", {"path": str(draft)}),
        _tc("read_pdf", {"path": str(pdf)}),
        _tc("format_check", {"path": str(draft)}),
        _tc("submit_review", {"path": str(draft), "verdict": "fail", "issues": issues}),
        Message(role="assistant", content="审查裁决：fail"),
    ])
    agent = make_agent(agent_registry, "reviewer", llm, cfg)
    result = await agent.run(f"审阅草稿文件 {draft}，对照原文 {pdf}")
    assert "审查裁决：fail" in result


@pytest.mark.asyncio
async def test_reviewer_checks_requirements_from_task(agent_env, agent_registry):
    """要求符合度：任务文本含「用户要求」→ reviewer 产出 requirements 维度 blocking。"""
    cfg, _ = agent_env
    scratch = Path(cfg.workspace) / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    draft = scratch / "review_test.md"
    draft.write_text("# 标题\n## 概述\n## 方法\n", encoding="utf-8")
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    issues = [{"severity": "blocking", "dimension": "requirements",
               "location": "概述", "action": "压缩到 500 字以内"}]
    llm = make_mock_llm([
        _tc("read_file", {"path": str(draft)}),
        _tc("read_pdf", {"path": str(pdf)}),
        _tc("format_check", {"path": str(draft)}),
        _tc("submit_review", {"path": str(draft), "verdict": "fail", "issues": issues}),
        Message(role="assistant", content="审查裁决：fail"),
    ])
    agent = make_agent(agent_registry, "reviewer", llm, cfg)
    result = await agent.run(f"审阅草稿文件 {draft}，对照原文 {pdf}。用户要求：500字以内")
    assert "审查裁决：fail" in result
