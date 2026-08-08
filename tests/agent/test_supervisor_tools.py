"""Supervisor 4 个调度工具测试（mock child，无真实 LLM）。"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperflow.core.agent import Agent, MaxTurnsExceeded, StreamEvent
from paperflow.core.llm import Message
from paperflow.core.security import PolicyEngineMiddleware
from paperflow.core.tool import Tool, ToolResult
from tests.conftest import _tc
from tests.agent.test_agent import make_mock_llm, make_mock_registry

from paperflow.tools.spawn import (
    SpawnSubAgentTool, ParallelSpawnTool, SubAgentResult,
    _UserWaitClock, _wrap_confirm_callback, _run_child_with_budget,
    _task_fingerprint, _task_has_path, _SPAWN_REGISTRY,
    _evict_stale_spawn_entries, _SPAWN_REUSE_WINDOW_S,
)
from agents.supervisor.tools import AskUserTool


def _supervisor(tools, **kwargs):
    """构造带指定工具的 Agent（agent_type="supervisor"），可注入 confirm/ask 回调。

    默认 llm 是空响应 mock——这些测试直接调 tool.execute()，不走 Agent.run()。"""
    llm = kwargs.pop("llm", None) or make_mock_llm([])
    return Agent(llm=llm, agent_registry=make_mock_registry(tools),
                 agent_type="supervisor", **kwargs)


# ─── B3 同 session 同指纹去重（Task 10，路径门控最小缓存）───────────────
# 主防线是 SKILL「同一意图不重复 spawn」；_SPAWN_REGISTRY 是机械安全网。指纹 = 纯文本
# sha256（规范化文本，零 I/O、不含内容快照）。路径门控（route 3，openclaw 式）：
# 无路径任务（纯文本，世界不变）→ running + done<5min 全量去重；有路径任务（引用真实
# 文件，世界可变）→ 只 running 去重、完成即清条目、永不缓存 done。running（in-flight）
# 命中 → 提示等待。注册表键为 session_id，去重只作用于同一会话内（并行 supervisor 各自
# 独立会话互不干扰）。


def test_task_fingerprint_normalizes_whitespace():
    """空白/换行差异的任务文本应映射同一指纹（去重的前提是规范化）。"""
    assert _task_fingerprint("  a\n b  ") == _task_fingerprint("a b")


def test_spawn_dedup_reuses_completed():
    """B3 done 缓存复用（无路径任务）：同 session 同指纹第二次 spawn 返回缓存，子 agent 不重构造。

    无路径任务（纯文本，世界不变）→ done<5min 缓存可复用。第二次同文本命中缓存 → 返回
    同一 ToolResult。MockAgent.call_count==1 证明第二次未重新构造子 agent（若去重失效，
    第二次会再构造 → call_count==2）。"""
    task = "审阅这份草稿并给出意见"
    with patch("paperflow.tools.spawn.Agent") as MockAgent:
        MockAgent.return_value.run = AsyncMock(return_value="done")
        agent = _supervisor([SpawnSubAgentTool()])
        tool = agent.tools["spawn_sub_agent"]
        first = tool.execute(agent_type="reviewer", task=task)
        second = tool.execute(agent_type="reviewer", task=task)
    assert MockAgent.call_count == 1                # 缓存命中 → 第二次不重构造
    assert first.text == second.text
    assert json.loads(first.text)["status"] == "success"
    assert json.loads(first.text)["summary"] == "done"


def test_spawn_dedup_reruns_when_world_changed(tmp_path):
    """路径门控回归（writer 再审锁）：含路径任务同文本二次 spawn 永不命中 done 缓存。

    过去靠内容快照感知「文件内容变 → 指纹变 → miss」；route 3 改为路径门控——含路径任务
    完成即清条目、永不缓存 done，文件内容根本不影响指纹。这里保留「改文件内容」叙事的
    回归锁：再审同任务文本（edit_file 修订后）必须拿到新 child 输出（pass），而非第一次
    的 fail 缓存。MockAgent.call_count==2 证明第二次真重构造 child。"""
    p = tmp_path / "draft.md"
    p.write_text("v1", encoding="utf-8")
    task = f"审阅草稿文件 {p}，对照原文 {tmp_path / 'paper.pdf'}"
    with patch("paperflow.tools.spawn.Agent") as MockAgent:
        MockAgent.return_value.run = AsyncMock(side_effect=["fail", "pass"])
        agent = _supervisor([SpawnSubAgentTool()])
        tool = agent.tools["spawn_sub_agent"]
        first = tool.execute(agent_type="reviewer", task=task)
        p.write_text("v2", encoding="utf-8")        # edit_file 修订草稿 → 世界变
        second = tool.execute(agent_type="reviewer", task=task)
    assert MockAgent.call_count == 2                # 缓存 miss → 第二次真重构造 child
    assert json.loads(first.text)["summary"] == "fail"
    assert json.loads(second.text)["summary"] == "pass"


def test_spawn_dedup_running_hint():
    """B3 running 去重：注册表已有 running 记录 → 提示等待，不构造 child。

    running 状态无法在单线程同步调用里自然构造（execute 同步跑完才返回），故直接
    seed 一条 running 记录到注册表，验证读路径返回等待提示而非重复派发。"""
    with patch("paperflow.tools.spawn.Agent") as MockAgent:
        MockAgent.return_value.run = AsyncMock(return_value="done")
        agent = _supervisor([SpawnSubAgentTool()])
        tool = agent.tools["spawn_sub_agent"]
        fp = _task_fingerprint("搜索 x")
        _SPAWN_REGISTRY.setdefault(agent.session_id, {})[fp] = {
            "state": "running", "result": None, "started_at": time.monotonic(),
        }
        result = tool.execute(agent_type="searcher", task="搜索 x")
    assert "正在执行中" in result.text
    MockAgent.assert_not_called()


def test_task_has_path_boolean():
    r"""_task_has_path 布尔判据：含路径 → True；纯文本 → False；散文 "/" 假阳性 → 保守 True。

    route 3 门控依赖此布尔判断区分「有路径任务（只 running 去重）」与「无路径任务（可 done
    缓存）」。安全方向：散文里的 "/"（如 "/5 评分"）误判为路径（假阳性）→ 跳过 done 缓存 →
    安全重跑。lookbehind 放宽为「前接非英文单词字符」（review Important 修复）：LLM 拼装的
    任务文本可能省略空格，路径前是标点（全角冒号/左括号/反引号）或中文时也能识别；而
    `Q1/Q2`/`8/10` 等散文斜杠（前接 word char）仍忽略。"""
    assert _task_has_path("/Users/x/draft.md")                     # 裸绝对路径
    assert _task_has_path("审阅草稿文件 /tmp/x/draft.md")           # 模板：路径跟在空格后
    assert _task_has_path("对照 /tmp/x/paper(v2).pdf 审查")         # 半角括号路径仍被识别
    # review Important 修复：旧 (?<!\S) 守卫要求路径前是空白，LLM 拼装省略空格时这些紧邻
    # 路径全被判 False → 真路径被当无路径 → done 缓存启用（route 3 要躲的 bug 复活）
    assert _task_has_path("审阅草稿文件：/tmp/x/draft.md")           # 全角冒号紧邻
    assert _task_has_path("(/tmp/x/draft.md)")                     # 左括号紧邻
    assert _task_has_path("`/tmp/x/draft.md`")                     # 反引号紧邻
    assert _task_has_path("审阅/tmp/x")                            # 中文紧邻 → 假阳性保守 True
    # 散文斜杠（前接英文单词字符）仍忽略
    assert not _task_has_path("Q1/Q2")
    assert not _task_has_path("今天是 8/10 号")
    assert not _task_has_path("a/b 是比例")
    assert not _task_has_path("搜索关于强化学习的论文")              # 纯文本无 "/" → False
    assert not _task_has_path("请给出 5 分评价")                    # 无 "/" → False
    assert _task_has_path("请给出 /5 分评价")                       # 散文 "/"（空格前）→ 保守 True


def test_path_bearing_task_never_caches_done(tmp_path):
    """路径门控核心：含路径任务完成即清条目、永不缓存 done——同文本同内容二次 spawn 也重跑。

    与 test_spawn_dedup_reruns_when_world_changed 的区别：这里文件内容**未变**（世界未变），
    纯靠「任务含路径」这一布尔判据就拒绝 done 缓存——证明门控不依赖内容快照。同时断言
    注册表在完成后已无该指纹条目（完成即清，非残留 done 待超窗剔除）。"""
    p = tmp_path / "draft.md"
    p.write_text("v1", encoding="utf-8")
    task = f"审阅草稿文件 {p}"
    with patch("paperflow.tools.spawn.Agent") as MockAgent:
        MockAgent.return_value.run = AsyncMock(return_value="done")
        agent = _supervisor([SpawnSubAgentTool()])
        tool = agent.tools["spawn_sub_agent"]
        first = tool.execute(agent_type="reviewer", task=task)
        assert _task_fingerprint(task) not in _SPAWN_REGISTRY.get(agent.session_id, {})  # 完成即清
        second = tool.execute(agent_type="reviewer", task=task)
    assert MockAgent.call_count == 2                # 有路径 → 二次同文本同内容也真重构造
    assert json.loads(first.text)["summary"] == "done"
    assert json.loads(second.text)["summary"] == "done"


def test_pathless_task_done_cache_reuse():
    """路径门控：无路径任务完成后注册表确实写 done 条目（与有路径「完成即清」对照），窗内复用。

    test_spawn_dedup_reuses_completed 已锁复用行为；这里补充注册表状态断言——无路径任务
    完成后落 done（可被窗内同指纹命中），与 _SPAWN_REGISTRY 残留清理逻辑正交。"""
    task = "搜索关于强化学习的论文"
    with patch("paperflow.tools.spawn.Agent") as MockAgent:
        MockAgent.return_value.run = AsyncMock(return_value="done")
        agent = _supervisor([SpawnSubAgentTool()])
        tool = agent.tools["spawn_sub_agent"]
        first = tool.execute(agent_type="searcher", task=task)
        assert _SPAWN_REGISTRY[agent.session_id][_task_fingerprint(task)]["state"] == "done"  # 无路径写 done
        second = tool.execute(agent_type="searcher", task=task)
    assert MockAgent.call_count == 1                # done 缓存命中 → 第二次不重构造
    assert first.text == second.text


class TestSpawnSubAgentTool:
    def test_needs_parent_injected(self):
        tool = SpawnSubAgentTool()
        agent = _supervisor([tool])
        assert tool._parent is agent

    def test_confirm_callback_passed_to_child(self):
        """D6 关键断言：child 构造必须继承父 confirm_callback（writer 写盘靠它）。"""
        with patch("paperflow.tools.spawn.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(return_value="done")
            cb = lambda cr: True
            agent = _supervisor([SpawnSubAgentTool()], confirm_callback=cb)
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="searcher", task="搜索 x")
        kwargs = MockAgent.call_args.kwargs
        assert kwargs["confirm_callback"] is cb
        assert kwargs["agent_type"] == "searcher"
        assert kwargs["security_middleware"] == agent.security_middleware
        assert kwargs["session_id"] == agent.session_id
        parsed = json.loads(result.text)
        assert parsed["status"] == "success"
        assert parsed["summary"] == "done"

    def test_timeout_maps_to_timeout_status(self):
        old = SpawnSubAgentTool.timeout
        SpawnSubAgentTool.timeout = 0.05       # 覆盖 120s 默认（类属性，测试后还原防泄漏）
        try:
            with patch("paperflow.tools.spawn.Agent") as MockAgent:
                async def hang(*a, **k):
                    await asyncio.sleep(5)
                MockAgent.return_value.run = hang
                agent = _supervisor([SpawnSubAgentTool()])
                result = agent.tools["spawn_sub_agent"].execute(
                    agent_type="searcher", task="t")
            # M3：断言用执行期生效的 self.timeout（0.05）插值——写死 "120s" 会与
            # 类属性覆盖后的真实值漂移，无法防插值回归
            expected = f"SubAgent 在 {SpawnSubAgentTool.timeout}s 内未完成"
        finally:
            SpawnSubAgentTool.timeout = old
        parsed = json.loads(result.text)
        assert parsed["status"] == "timeout"
        assert parsed["error_detail"] == expected

    def test_max_turns_maps_to_failed(self):
        with patch("paperflow.tools.spawn.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(side_effect=MaxTurnsExceeded("boom"))
            agent = _supervisor([SpawnSubAgentTool()])
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="searcher", task="t")
        parsed = json.loads(result.text)
        assert parsed["status"] == "failed"
        assert "boom" in parsed["error_detail"]

    def test_non_supervisor_parent_denied(self):
        """allowed_spawns 运行时校验：非 supervisor parent 拒绝越界 spawn（ADR 0003）。"""
        tool = SpawnSubAgentTool()
        registry = make_mock_registry([tool])     # allowed_spawns 缺省 []
        agent = Agent(llm=make_mock_llm([], ), agent_registry=registry,
                      agent_type="writer")
        result = tool.execute(agent_type="reviewer", task="审稿")
        parsed = json.loads(result.text)
        assert parsed["status"] == "denied"
        assert "不能 spawn" in parsed["summary"]

    def test_resolve_timeout_map_and_fallback(self):
        """D2：config 按 agent 命中优先；未命中 fallback 到类默认（M3 seam 保留）。"""
        tool = SpawnSubAgentTool(agent_timeouts={"writer": 300})
        assert tool._resolve_timeout("writer") == 300
        assert tool._resolve_timeout("searcher") == 120      # 类默认

    def test_timeout_uses_agent_timeouts_map(self):
        """config 命中时 error_detail 插值用 map 值而非类默认（防漂移）。"""
        with patch("paperflow.tools.spawn.Agent") as MockAgent:
            async def hang(*a, **k):
                await asyncio.sleep(5)
            MockAgent.return_value.run = hang
            tool = SpawnSubAgentTool(agent_timeouts={"searcher": 0.05})
            agent = _supervisor([tool])
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="searcher", task="t")
        parsed = json.loads(result.text)
        assert parsed["status"] == "timeout"
        assert parsed["error_detail"] == "SubAgent 在 0.05s 内未完成"


class TestParallelSpawnTool:
    def test_per_child_isolation(self):
        """一个 child 失败只映射自身，不拖垮其他（spec 🟠3）。"""
        with patch("paperflow.tools.spawn.Agent") as MockAgent:
            async def run_mixed(task):
                if "fail" in task:
                    raise MaxTurnsExceeded("boom")
                return f"ok:{task}"
            MockAgent.return_value.run = run_mixed
            agent = _supervisor([ParallelSpawnTool()])
            result = agent.tools["parallel_spawn"].execute(spawns=[
                {"agent_type": "a", "task": "ok1"},
                {"agent_type": "b", "task": "fail"},
            ])
        parsed = json.loads(result.text)
        assert len(parsed) == 2
        by_status = {p["status"] for p in parsed}
        assert by_status == {"success", "failed"}

    def test_parallel_success_all(self):
        with patch("paperflow.tools.spawn.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(side_effect=lambda task: f"r:{task}")
            agent = _supervisor([ParallelSpawnTool()])
            result = agent.tools["parallel_spawn"].execute(spawns=[
                {"agent_type": "a", "task": "t1"},
                {"agent_type": "a", "task": "t2"},
            ])
        parsed = json.loads(result.text)
        assert [p["status"] for p in parsed] == ["success", "success"]

    def test_denied_when_spawn_not_allowed(self):
        """R1 修复：ParallelSpawn 越界 spawn 也返回 per-child denied（与 Spawn 校验对齐）。

        审阅发现 SpawnSubAgentTool 有 allowed_spawns 运行时校验，ParallelSpawnTool
        的 _run_one 却无条件构造 child——两工具不对称。此用例钉死：非 supervisor
        parent 的越界 spawn 必须 denied，且不构造 child（MockAgent 不被调用）。"""
        tool = ParallelSpawnTool()
        with patch("paperflow.tools.spawn.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(return_value="ok")
            registry = make_mock_registry([tool])     # allowed_spawns 缺省 []
            agent = Agent(llm=make_mock_llm([]), agent_registry=registry,
                          agent_type="writer")
            result = tool.execute(spawns=[
                {"agent_type": "a", "task": "t1"},
                {"agent_type": "b", "task": "t2"},
            ])
        parsed = json.loads(result.text)
        assert len(parsed) == 2
        assert [p["status"] for p in parsed] == ["denied", "denied"]
        assert all("不能 spawn" in p["summary"] for p in parsed)
        MockAgent.assert_not_called()

    def test_parallel_resolve_timeout_map(self):
        """Parallel 与 Spawn 对称：同款 _resolve_timeout（R1 对齐哲学延续）。"""
        tool = ParallelSpawnTool(agent_timeouts={"writer": 300})
        assert tool._resolve_timeout("writer") == 300
        assert tool._resolve_timeout("other") == 120


class TestAskUserTool:
    def test_callback_answer_becomes_result(self):
        tool = AskUserTool()
        calls = []
        def cb(q):
            calls.append(q)
            return "再搜索"
        tool._parent = MagicMock(ask_user_callback=cb)
        result = tool.execute(question="要搜索吗？")
        assert calls == ["要搜索吗？"]
        assert "再搜索" in result.text

    def test_failsafe_without_callback(self):
        tool = AskUserTool()
        tool._parent = MagicMock(ask_user_callback=None)
        result = tool.execute(question="要搜索吗？")
        assert "无法交互" in result.text


class TestStreamCallbackPropagation:
    def test_spawn_passes_stream_callback_to_child(self):
        """子 agent 继承父 stream_callback：单 spawn 全量流式的基础。"""
        cb = lambda ev: None
        with patch("paperflow.tools.spawn.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(return_value="done")
            agent = _supervisor([SpawnSubAgentTool()], stream_callback=cb)
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="searcher", task="搜索 x")
        kwargs = MockAgent.call_args.kwargs
        assert kwargs["stream_callback"] is cb
        assert "done" in json.loads(result.text)["summary"]

    def test_parallel_filters_content_and_prefixes_tool_events(self):
        """并行包装回调：content 丢弃、tool 加 [agent_type] 前缀（防多路 token 串字）。"""
        received = []
        parent_cb = received.append
        with patch("paperflow.tools.spawn.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(return_value="ok")
            agent = _supervisor([ParallelSpawnTool()], stream_callback=parent_cb)
            agent.tools["parallel_spawn"].execute(spawns=[
                {"agent_type": "a", "task": "t1"},
            ])
        child_cb = MockAgent.call_args.kwargs["stream_callback"]
        child_cb(StreamEvent("content", "推理文本", "a"))              # content 被丢弃
        assert received == []
        child_cb(StreamEvent("tool", "调用 search_arxiv(query=x)", "a"))  # tool 加前缀透传
        assert received == [StreamEvent("tool", "[a] 调用 search_arxiv(query=x)", "a")]


# ─── 确认等待排除在超时外（2026-08-07 用户决策）───────────────────────
# 用户确认是交互等待，不应计入子 agent 的执行预算：预算 = 基础 timeout + 累积用户等待。
# 三件套：_UserWaitClock（begin/end 记录进行中等待）/ _wrap_confirm_callback（外包计时）/
# _run_child_with_budget（把累积等待加回剩余预算，确认期间超时暂停）。


class _ConfirmWriteTool(Tool):
    """requires_confirm=True 的写盘工具：触发 PolicyEngine ConfirmRequired → confirm_callback。

    与 test_cli.py 的 ConfirmWriteTool 同形态（本地定义，避免测试文件间耦合）。"""
    name = "confirm_write"
    description = "写盘，需确认"
    parameters = {"type": "object", "properties": {}}
    requires_confirm = True

    def execute(self) -> ToolResult:
        return ToolResult(text="written")


class TestUserWaitBudget:
    @pytest.mark.asyncio
    async def test_budget_excludes_user_wait(self):
        """0.1s 预算下：0.3s 确认等待（clock.begin/end 记录）+ 0.05s 执行 → 完成不超时。"""
        clock = _UserWaitClock()
        async def coro():
            clock.begin()                      # 等价 confirm wrapper：确认开始
            await asyncio.sleep(0.3)           # 模拟等待用户确认
            clock.end()                        # 等价 wrapper finally：确认结束
            await asyncio.sleep(0.05)          # 确认后的执行
            return "done"
        result = await _run_child_with_budget(coro(), timeout=0.1, clock=clock)
        assert result == "done"

    @pytest.mark.asyncio
    async def test_budget_times_out_without_user_wait(self):
        """纯执行超时仍触发：0.1s 预算下 0.5s 执行（无确认）→ TimeoutError（防误放行回归）。"""
        clock = _UserWaitClock()
        async def coro():
            await asyncio.sleep(0.5)
        with pytest.raises(asyncio.TimeoutError):
            await _run_child_with_budget(coro(), timeout=0.1, clock=clock)

    @pytest.mark.asyncio
    async def test_wrap_confirm_records_wait(self):
        """confirm wrapper 把等待时长记入 clock（进行中 + 结束时 total 都反映等待）。"""
        clock = _UserWaitClock()
        async def orig(cr):
            await asyncio.sleep(0.1)
            return True
        wrapped = _wrap_confirm_callback(orig, clock)
        assert await wrapped(None) is True
        assert clock.total() >= 0.1


class TestSpawnConfirmBudget:
    def test_confirm_wait_excluded_from_timeout(self):
        """集成：子 agent 写盘需确认（确认等待 0.2s > 预算 0.1s），确认后 success。

        回归锁死 2026-08-07 修复：若确认等待计入预算（旧 wait_for 纯墙钟），0.1s 预算下
        子 agent 在确认中途被误杀为 timeout；排除后预算延长，确认完成 → 正常 success。
        真实子 agent（真实 PolicyEngineMiddleware + confirm_callback 慢确认）经真实
        SpawnSubAgentTool._run_child 走完整链路。"""
        child_llm = make_mock_llm([
            _tc("confirm_write", {}),
            Message(role="assistant", content="写好了"),
        ])
        async def slow_confirm(cr):
            await asyncio.sleep(0.2)
            return True
        old = SpawnSubAgentTool.timeout
        SpawnSubAgentTool.timeout = 0.1
        try:
            agent = _supervisor(
                [SpawnSubAgentTool(), _ConfirmWriteTool()],
                llm=child_llm,
                security_middleware=[PolicyEngineMiddleware()],
                confirm_callback=slow_confirm,
            )
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="searcher", task="写笔记")
        finally:
            SpawnSubAgentTool.timeout = old
        parsed = json.loads(result.text)
        assert parsed["status"] == "success"
        assert "写好了" in parsed["summary"]


# ─── C2 结构化摘要聚合（Task 11）────────────────────────────────────
# digest = StructuredOutput 从子 agent 最终回答提取的结构化摘要（如 searcher 的
# count/papers/downloaded），supervisor 直接读 digest 组织最终回答，取代 aggregate_results。
# 失败/超时落 {}（supervisor 回退读 summary）。mock llm 的 chat 不接受 json_mode/
# temperature/extra_body → 真实提取在测试里 TypeError 静默降级为 {}，不破坏既有 spawn 用例。


def test_subagent_result_has_digest_field():
    from paperflow.tools.spawn import SubAgentResult
    r = SubAgentResult(status="success", summary="x")
    assert r.digest == {}


def test_digest_schema_for_maps_types():
    from paperflow.tools.spawn import digest_schema_for
    from paperflow.tools.spawn import (
        SearcherDigest, ReviewerDigest, WriterDigest, GenericDigest,
    )
    assert digest_schema_for("searcher") is SearcherDigest
    assert digest_schema_for("reviewer") is ReviewerDigest
    assert digest_schema_for("writer") is WriterDigest
    assert digest_schema_for("unknown-x") is GenericDigest


def test_aggregate_tool_removed():
    """C2：aggregate_results 工具已删除——supervisor 直接读各 spawn 结果的 digest。"""
    import agents.supervisor.tools as st
    assert not hasattr(st, "AggregateResultsTool")
    from paperflow.tools.spawn import SpawnSubAgentTool
    assert True


def _digest_llm(content: str):
    """返回接受 json_mode/temperature/extra_body 的 mock chat，固定返回给定 JSON content。

    StructuredOutput.extract 会以这三个 kwarg 调 llm.chat；共享 make_mock_llm 的
    chat 签名不含它们 → 既有的 digest 提取在测试里恒走 {} 兜底（TypeError 被捕获）。
    本 helper 是"真实路径"的专用 mock：不碰共享 make_mock_llm（改了会让按序消费
    mock 的既有用例漂移），只在此锁定 StructuredOutput/schema 交互与 model_dump。
    """
    from unittest.mock import MagicMock
    from paperflow.core.llm import Message
    m = MagicMock()
    async def chat(messages, tools=None, tool_choice="auto", json_mode=False,
                   temperature=None, extra_body=None):
        return Message(role="assistant", content=content)
    m.chat = chat
    m.model = "mock"
    return m


def test_extract_digest_structured_success():
    """真实路径（review Important 回归锁）：合法 JSON → 提取为 schema 默认值补齐的 dict。

    SearcherDigest 的 downloaded/pending_confirm/needs_attention 是可选字段，pydantic
    model_dump 会补齐默认值——断言整个 dict（含默认值）而非部分键，锁住 model_dump 回归。"""
    import asyncio
    import json
    from paperflow.tools.spawn import _extract_digest
    llm = _digest_llm(json.dumps({"count": 2, "papers": ["a", "b"]}))
    d = asyncio.run(_extract_digest(llm, "searcher", "一些最终回答文本"))
    assert d == {"count": 2, "papers": ["a", "b"],
                 "downloaded": [], "pending_confirm": [], "needs_attention": False}


def test_extract_digest_invalid_json_falls_back_empty():
    """真实路径兜底（review Important 回归锁）：非法 JSON 走 except Exception → {}。

    验证 _extract_digest 的失败兜底不是"mock 签名 TypeError 才触发"——即使 chat 接受
    全部 kwarg、返回非法 JSON，也应静默返回 {}（supervisor 回退读 summary）。"""
    import asyncio
    from paperflow.tools.spawn import _extract_digest
    llm = _digest_llm("不是 JSON")
    d = asyncio.run(_extract_digest(llm, "searcher", "文本"))
    assert d == {}


def test_evict_stale_spawn_entries_removes_only_expired_done():
    """注册表内存卫生（review finding）：超窗 done 条目剔除、新鲜 done 与 running 保留。

    done 条目永不清洗会导致长会话按指纹数无限累积 ToolResult；但 running 条目
    （可能正被另一 worker 线程执行）绝不可删——删了并发去重原子性失效。"""
    now = time.monotonic()
    reg = {
        "old-done": {"state": "done", "result": "旧结果",
                     "started_at": now - _SPAWN_REUSE_WINDOW_S - 1},   # 超窗 → 应剔除
        "fresh-done": {"state": "done", "result": "新结果",
                       "started_at": now},                              # 窗内 → 保留
        "running": {"state": "running", "result": None, "started_at": now - 9999},  # 超窗仍保留
    }
    _evict_stale_spawn_entries(reg, now)
    assert "old-done" not in reg
    assert "fresh-done" in reg
    assert "running" in reg
