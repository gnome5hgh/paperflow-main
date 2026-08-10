"""reviewer 审稿 agent 测试：mock LLM 驱动 ReAct，工具经真实安全链执行。

reviewer 是 review-note 改造后的两模式 agent（Task 6）：
- 笔记审查模式（任务前缀「审阅草稿文件…」）：沿用原 review-note 流程，收尾 submit_review；
- 下载审查模式（任务前缀「审查以下候选论文…」）：下载/推荐前门禁，逐篇核验后
  submit_download_review 交裁决。
两个测试分别覆盖：① 装配断言（8 工具并集，含 Task 4/5 新工具）；② 下载模式端到端
（mock LLM 顺序消费，lookup_venue_rank 走本地映射表——"WWW" 命中，无网络）。
"""
import pytest

from paperflow.core.llm import Message
from tests.conftest import make_mock_llm, _tc, make_agent


def test_reviewer_notes_mode_tools_assembled(agent_env, agent_registry):
    """装配断言：reviewer 两模式工具并集 = 笔记审查（read_file/read_pdf/format_check/submit_review）
    + 下载审查（lookup_venue_rank/submit_download_review）+ 定位核对（glob/grep）共 8 工具。

    Task 4/5 新工具（lookup_venue_rank / submit_download_review）经 paperflow.tools.__init__
    导出后，reviewer.tools.py 才能 import——本断言同时防 Task 4/5 接线回退。"""
    cfg, _ = agent_env
    agent = make_agent(agent_registry, "reviewer", make_mock_llm([]), cfg)
    names = set(agent.tools)
    assert {"read_file", "read_pdf", "format_check", "submit_review",
            "lookup_venue_rank", "submit_download_review", "glob", "grep"} <= names


@pytest.mark.asyncio
async def test_reviewer_download_mode_submits_verdict(agent_env, agent_registry):
    """下载模式端到端：审查候选论文 → lookup_venue_rank 查等级 → submit_download_review → 结束。

    mock 序列与真实 ReAct 对齐（同 test_agent_review.py happy path 风格）：每条 `_tc` 是
    LLM 的一轮 tool_call，工具结果由真实执行产生（lookup_venue_rank 命中本地映射表
    "WWW"→CCF-A，无网络；submit_download_review 校验 pass 语义后格式化裁决），
    最后一条纯 content 消息终止循环。brief 原稿 mock 列表里夹的 `Message(role="tool")`
    条目不适用于本项目 make_mock_llm（盲 pop(0) 顺序消费，tool 角色消息无 tool_calls
    会让 ReAct 提前终止）——按项目既有异步测试写法去掉。"""
    cfg, _ = agent_env
    responses = [
        _tc("lookup_venue_rank", {"venue": "WWW"}),
        _tc("submit_download_review", {"verdict": "pass", "items": [{
            "title": "HGT", "venue_rank": {"ccf": "A"}, "decision": "pass",
            "reasons": ["等级通过"], "source_link": "https://x"}]}),
        Message(role="assistant", content="审查裁决：pass\n可下载：HGT"),
    ]
    llm = make_mock_llm(responses)
    agent = make_agent(agent_registry, "reviewer", llm, cfg)
    out = await agent.run(
        "审查以下候选论文：HGT(WWW 2020)。要求：年份≥2026，等级≥Q2，主题=异构图特征提取"
    )
    assert "审查裁决：pass" in out


@pytest.mark.asyncio
async def test_reviewer_download_mode_no_rank_constraint(agent_env, agent_registry):
    """无等级约束场景：任务文本不含等级 → reviewer 不调 lookup_venue_rank，预印本项 pass。

    等级门禁条件化后，用户未要求等级时 reviewer 跳过等级维度，
    直接对预印本项交 submit_download_review（items 无 venue_rank）。mock LLM 序列
    不出现 lookup_venue_rank，锁住该流程形态；SKILL 实际提示效果由 smoke 验证。"""
    cfg, _ = agent_env
    responses = [
        _tc("submit_download_review", {"verdict": "pass", "items": [{
            "title": "Quantum negative sampling for KGE",
            "decision": "pass",
            "reasons": ["年份≥2025", "主题相关", "预印本无等级要求"],
            "source_link": "https://arxiv.org/pdf/2502.17973",
        }]}),
        Message(role="assistant", content="审查裁决：pass\n预印本：Quantum negative sampling for KGE"),
    ]
    llm = make_mock_llm(responses)
    agent = make_agent(agent_registry, "reviewer", llm, cfg)
    out = await agent.run(
        "审查以下候选论文：Quantum negative sampling(arXiv, 2025)。用户约束：年份≥2025、主题=负采样算法"
    )
    assert "审查裁决：pass" in out


def test_reviewer_lacks_ask_user_question(agent_registry):
    """权限最小化：reviewer 不装配 ask_user_question（审核标准由任务文本给定）。

    反面断言锁装配面——防回归：reviewer 审核依据是任务文本里的约束，
    不该有中途问用户入口；上面的 `<=` 子集断言测不出新增工具，这条 not in 拦住。"""
    config = agent_registry.get_config("reviewer")
    names = {t.name for t in config.tools}
    assert "ask_user_question" not in names
