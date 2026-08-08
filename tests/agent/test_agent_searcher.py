"""searcher 搜索 agent 测试：mock LLM 驱动，搜索工具注入 MockTransport 隔离网络。"""
import httpx
import pytest

from paperflow.core.llm import Message
from paperflow.tools import ArxivSearchTool, OpenAlexSearchTool
from tests.conftest import make_mock_llm, _tc, make_agent


@pytest.fixture(autouse=True)
def _no_real_redirect(monkeypatch):
    """跳过 resolve_url_target 的真实网络调用（保持测试封闭）。

    _get 里 resolve_url_target 会发起真实 HEAD 请求并走真实 DNS——与
    "绝不真实网络"约束冲突（且沙箱外网解析到私网段/超时，行为不确定）。
    桩替换为恒等函数，让 httpx.MockTransport 罐头响应成为唯一网络来源；
    生产环境仍保留真实的逐跳重定向 SSRF 防护（此处仅测试隔离）。
    """
    monkeypatch.setattr("paperflow.tools._http.resolve_url_target", lambda u: u)

_ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Graph Neural Networks for Link Prediction</title>
<id>http://arxiv.org/abs/2101.00001v1</id>
<published>2021-01-01T00:00:00Z</published>
<summary>Abstract text here.</summary>
</entry></feed>"""
_OPENALEX = {"results": [{
    "id": "https://openalex.org/W1", "display_name": "circRNA regulation",
    "publication_year": 2022, "cited_by_count": 30,
    "abstract_inverted_index": {"circRNA": [0]},
}]}


def _atransport():
    return httpx.MockTransport(lambda req: httpx.Response(200, content=_ATOM.encode()))


def _otransport():
    return httpx.MockTransport(lambda req: httpx.Response(200, json=_OPENALEX))


@pytest.mark.asyncio
async def test_search_paper_happy_path(agent_env, agent_registry):
    cfg, _ = agent_env
    llm = make_mock_llm([
        _tc("arxiv_search", {"query": "link prediction", "max_results": 3}),
        _tc("openalex_search", {"query": "link prediction", "max_results": 3}),
        # Task 8 门禁：候选收敛后 spawn reviewer 下载审查（取代 dedup_papers/filter_papers）——
        # 去重已并入池插入逻辑（search/_common.py），筛选并入 reviewer 逐篇核验。
        _tc("spawn_sub_agent", {"agent_type": "reviewer",
                                "task": "审查以下候选论文：[...] 用户约束：年份≥2020"}),
        Message(role="assistant", content="审查裁决：pass\n- [PASS] Graph Neural Networks | Q1 | 可下载"),
        Message(role="assistant", content="找到 1 篇论文"),
    ])
    agent = make_agent(agent_registry, "searcher", llm, cfg)
    # 注入 mock transport：搜索工具 execute 会真打网络，必须隔离（ssrf_check 桩跳过 DNS 校验）
    agent.tools["arxiv_search"]._client = ArxivSearchTool._make_client(transport=_atransport(), ssrf_check=lambda u: None)
    agent.tools["openalex_search"]._client = OpenAlexSearchTool._make_client(transport=_otransport(), ssrf_check=lambda u: None)
    result = await agent.run("搜索链路预测论文")
    assert "找到 1 篇论文" in result


@pytest.mark.asyncio
async def test_search_paper_single_source_failure(agent_env, agent_registry):
    cfg, _ = agent_env
    from paperflow.core.security.network import SSRFError

    def blocking(url):
        raise SSRFError("blocked for test")

    llm = make_mock_llm([
        _tc("arxiv_search", {"query": "x", "max_results": 3}),
        # 注意：arxiv 被 SSRF 拦截后，mock LLM 下一轮直接转 openalex_search
        # （真实模型会先输出叙述再调用工具；mock 框架一轮一个 Message，
        #   纯 content 消息会让 ReAct 循环提前终止——此处省略叙述）
        _tc("openalex_search", {"query": "x", "max_results": 3}),
        Message(role="assistant", content="OpenAlex 也无结果"),
    ])
    agent = make_agent(agent_registry, "searcher", llm, cfg)
    agent.tools["arxiv_search"]._client = ArxivSearchTool._make_client(transport=_atransport(), ssrf_check=blocking)
    agent.tools["openalex_search"]._client = OpenAlexSearchTool._make_client(transport=_otransport(), ssrf_check=lambda u: None)
    result = await agent.run("搜索 x")
    assert "无结果" in result


def test_search_paper_has_glob_grep(agent_registry):
    """Task 4：searcher 装配 glob/grep——枚举已下载 PDF、下载前去重、内容校验。

    searcher 不再盲猜论文精确路径（P2 路径风暴根因）：glob 按模式枚举
    vault 内已下载 PDF（决定要不要重新下载），grep 校验下载后内容锚点。
    只读工具 risk=low，无确认门——装配不进安全边界即可，名单断言防回归。"""
    config = agent_registry.get_config("searcher")
    names = {t.name for t in config.tools}
    assert {"glob", "grep"} <= names


def test_search_paper_tools_without_dedup_filter(agent_registry, agent_env):
    """Task 8 工具集变化：门禁管线工具 = 双源搜索 + fetch_pdf + glob/grep + spawn。

    去重/筛选工具已删除——去重并入池插入逻辑（search/_common.py），筛选并入
    reviewer 下载审查门禁（agents/reviewer）。下载自搜索拆为独立 fetch_pdf 工具
    （audit 中写盘动作清晰归于下载工具）。spawn_sub_agent 是门禁关键工具：
    候选收敛后派发 reviewer 逐篇核验（allowed_spawns 声明 reviewer 才放行，
    _check_spawn_allowed 运行时校验）。"""
    cfg, _ = agent_env
    from tests.conftest import make_agent, make_mock_llm
    agent = make_agent(agent_registry, "searcher", make_mock_llm([]), cfg)
    names = set(agent.tools)
    assert {"arxiv_search", "openalex_search", "fetch_pdf", "glob", "grep", "spawn_sub_agent"} <= names
    assert "dedup_papers" not in names and "filter_papers" not in names
    cfg2 = agent.agent_registry.get_config("searcher")
    assert cfg2.allowed_spawns == ["reviewer"]
