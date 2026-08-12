"""searcher 搜索 agent 测试：mock LLM 驱动，搜索工具注入 MockTransport 隔离网络。"""
import httpx
import pytest

from paperflow.core.llm import Message
from paperflow.tools import WebSearchTool
from tests.conftest import make_mock_llm, _tc, make_agent


@pytest.fixture(autouse=True)
def _no_real_redirect(monkeypatch):
    """跳过 resolve_url_target 的真实网络调用（保持测试封闭）。

    _get 里 resolve_url_target 会发起真实 HEAD 请求并走真实 DNS——与
    "绝不真实网络"约束冲突。桩替换为恒等函数，让 httpx.MockTransport 罐头
    响应成为唯一网络来源；生产环境仍保留真实的逐跳重定向 SSRF 防护。
    """
    monkeypatch.setattr("paperflow.tools.common._http.resolve_url_target", lambda u: u)

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


def _inject(agent, source, transport, ssrf_check=lambda u: None):
    """把 MockTransport 注入 agent 上 web_search 工具的某 source 客户端。"""
    tool = agent.tools["web_search"]
    tool._clients[source] = WebSearchTool._make_client(source, transport=transport,
                                                       ssrf_check=ssrf_check)


@pytest.mark.asyncio
async def test_search_paper_happy_path(agent_env, agent_registry):
    cfg, _ = agent_env
    llm = make_mock_llm([
        _tc("web_search", {"query": "link prediction", "source": "arxiv", "max_results": 3}),
        _tc("web_search", {"query": "link prediction", "source": "openalex", "max_results": 3}),
        # 门禁：候选收敛后 spawn reviewer 下载审查（去重并入池插入逻辑，
        # 筛选并入 reviewer 逐篇核验）
        _tc("spawn_sub_agent", {"agent_type": "reviewer", "mode": "download_review",
                                "task": "审查以下候选论文：[...] 用户约束：年份≥2020"}),
        Message(role="assistant", content="审查裁决：pass\n- [PASS] Graph Neural Networks | Q1 | 可下载"),
        Message(role="assistant", content="找到 1 篇论文"),
    ])
    agent = make_agent(agent_registry, "searcher", llm, cfg)
    # 注入 mock transport：web_search 的每个 source 客户端都需隔离（ssrf_check 桩跳过 DNS）
    _inject(agent, "arxiv", _atransport())
    _inject(agent, "openalex", _otransport())
    result = await agent.run("搜索链路预测论文")
    assert "找到 1 篇论文" in result


@pytest.mark.asyncio
async def test_search_paper_single_source_failure(agent_env, agent_registry):
    cfg, _ = agent_env
    from paperflow.core.security.network import SSRFError

    def blocking(url):
        raise SSRFError("blocked for test")

    llm = make_mock_llm([
        _tc("web_search", {"query": "x", "source": "arxiv", "max_results": 3}),
        # 注意：arxiv 被 SSRF 拦截后，mock LLM 下一轮直接转 openalex
        #（真实模型会先输出叙述再调用工具；mock 框架一轮一个 Message，
        #   纯 content 消息会让 ReAct 循环提前终止——此处省略叙述）
        _tc("web_search", {"query": "x", "source": "openalex", "max_results": 3}),
        Message(role="assistant", content="OpenAlex 也无结果"),
    ])
    agent = make_agent(agent_registry, "searcher", llm, cfg)
    _inject(agent, "arxiv", _atransport(), ssrf_check=blocking)
    _inject(agent, "openalex", _otransport())
    result = await agent.run("搜索 x")
    assert "无结果" in result


def test_search_paper_has_glob_grep(agent_registry):
    """searcher 装配 glob/grep——枚举已下载 PDF、下载前去重、内容校验。"""
    config = agent_registry.get_config("searcher")
    names = {t.name for t in config.tools}
    assert {"glob", "grep"} <= names


def test_searcher_has_ask_user_question_for_recommendation(agent_registry):
    """searcher 装配 ask_user_question——推荐后询问用户是否加入未读清单。

    这是推荐询问的既有模式（writer/qa-agent 同款：任务中途向用户提问，in-turn
    阻塞，答案即回子任务），不是权限回退——工具可问用户但不可直接调度业务子
    agent（allowed_spawns 仍只放行 reviewer）。"""
    config = agent_registry.get_config("searcher")
    names = {t.name for t in config.tools}
    assert "ask_user_question" in names


def test_search_paper_tools_without_dedup_filter(agent_registry, agent_env):
    """searcher 工具集 = 通用 web_search + fetch_pdf + glob/grep + spawn。

    去重/筛选工具已删除——去重并入池插入逻辑（search/_common.py），筛选并入
    reviewer 下载审查门禁。下载自搜索拆为独立 fetch_pdf。旧双源工具名不再存在。"""
    cfg, _ = agent_env
    from tests.conftest import make_agent, make_mock_llm
    agent = make_agent(agent_registry, "searcher", make_mock_llm([]), cfg)
    names = set(agent.tools)
    assert {"web_search", "fetch_pdf", "glob", "grep", "spawn_sub_agent"} <= names
    assert "arxiv_search" not in names and "openalex_search" not in names
    assert "dedup_papers" not in names and "filter_papers" not in names
    cfg2 = agent.agent_registry.get_config("searcher")
    assert cfg2.allowed_spawns == ["reviewer"]
