"""writer agent + spawn 审稿测试（Task 7：删 ReviewDraftTool 桥，改共享 SpawnSubAgentTool）。

审稿由 review_draft 桥（agent 目录内单消费者工具）改为直接 spawn_sub_agent
（paperflow/tools/orchestration/spawn.py 共享层）——与 supervisor 同款派发。父 writer
在 ReAct 循环里调用 spawn_sub_agent(agent_type=reviewer, task="审阅草稿文件 <draft>，对照原文 <pdf>")，
reviewer 子 agent 返回 SubAgentResult（summary 首行「审查裁决：pass/fail」）。
"""
import json
from pathlib import Path

import pytest

from paperflow.core.llm import Message
from tests.conftest import make_mock_llm, _tc, make_agent


def test_generate_note_tools_review_via_spawn(agent_registry, agent_env):
    """writer 不再有 review_draft，改用共享 spawn_sub_agent 派发 reviewer 审稿。

    Task 7 核心断言：审稿桥删除、共享 spawn 工具装配、allowed_spawns 声明 reviewer。"""
    cfg, _ = agent_env
    agent = make_agent(agent_registry, "writer", make_mock_llm([]), cfg)
    assert "review_draft" not in agent.tools
    assert "spawn_sub_agent" in agent.tools
    cfg2 = agent.agent_registry.get_config("writer")
    assert cfg2.allowed_spawns == ["reviewer"]


def test_generate_note_tools_metadata(agent_env, agent_registry):
    """writer 完整装配：9 工具齐备（5 原子 + spawn_sub_agent + glob/grep + rag_retrieve）。

    review_draft 桥删除 → spawn_sub_agent（共享 SpawnSubAgentTool）替代；
    spawn_sub_agent 是唯一需要 parent 注入的工具（嵌套 spawn 子 agent），其余原子
    工具不需要——权限最小化。glob/grep 保留（文件名定位 + 文本锚点），另装
    rag_retrieve 服务大纲模式的笔记发现与段落回溯。"""
    config = agent_registry.get_config("writer")
    names = [t.name for t in config.tools]
    assert "review_draft" not in names
    assert "spawn_sub_agent" in names
    assert "read_file" in names and "write_file" in names
    assert "glob" in names and "grep" in names      # Task 4：文件名定位 + 文本锚点
    spawn = next(t for t in config.tools if t.name == "spawn_sub_agent")
    assert spawn.needs_parent is True


@pytest.mark.asyncio
async def test_generate_note_single_round(agent_env, agent_registry):
    """单轮 happy path：读模板 → 读 PDF → write_file 落盘草稿 v1 → spawn reviewer 审稿通过 → 定稿。

    Task 7：审稿由 review_draft 桥改为 spawn_sub_agent(agent_type=reviewer, task=…)——
    父 writer 用共享 spawn 工具派发 reviewer 子 agent；子 agent（mock LLM）
    首轮即回「审查裁决：pass」，父不再循环直接定稿。草稿 v1 直接 write_file 到最终
    路径（vault note），spawn 任务文本传 draft_path（不再把整篇草稿塞进工具参数）。
    make_agent 接真实安全链，write_file requires_confirm=True → confirm_callback 自动
    接受，落盘触发 index_document（patch 的 svc + FakeEmbedder）。"""
    cfg, _ = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    note_out = Path(cfg.vault_note_dir) / "paper.md"
    template = Path(cfg.workspace) / "templates" / "paper_note.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("# 标题\n## 概述\n## 方法\n", encoding="utf-8")

    task = f"审阅草稿文件 {note_out}，对照原文 {pdf}"
    llm = make_mock_llm([
        _tc("read_file", {"path": str(template)}),
        _tc("read_pdf", {"path": str(pdf)}),
        _tc("write_file", {"path": str(note_out), "content": "# 标题\n## 概述\n## 方法\n"}),   # 草稿 v1 → 最终路径
        _tc("spawn_sub_agent", {"agent_type": "reviewer", "mode": "note_review", "task": task}),
        Message(role="assistant", content="审查裁决：pass"),   # reviewer 子 agent 首轮即过
        Message(role="assistant", content="笔记已生成"),
    ])
    agent = make_agent(agent_registry, "writer", llm, cfg)
    result = await agent.run(f"为 {pdf} 生成笔记")
    assert "笔记已生成" in result
    assert note_out.exists()              # 定稿落盘（confirm_callback 已接受 write_file）


class LoopMockLLM:
    """e2e 专用 mock：预设响应序列；callable 项接收 messages 动态构造。

    子 agent 的 read_file 轮次需要 draft_path（A-ii 下固定为 note_out，但父测试
    仍从子 agent（reviewer 笔记审查模式）的 user 任务文本"审阅草稿文件 <draft>，
    对照原文 <pdf>"动态解析，与生产行为一致）。"""

    def __init__(self):
        self.responses = []
        self.seen_tasks = []
        self.callable_hits = 0

    def add(self, resp):
        self.responses.append(resp)

    async def chat(self, messages, tools=None, tool_choice="auto", telemetry_callback=None):
        for m in messages:
            if m.role == "user":
                self.seen_tasks.append(m.content)
        resp = self.responses.pop(0)
        if callable(resp):
            self.callable_hits += 1
            return resp(messages)
        return resp

    model = "mock"


def child_read(messages):
    """子 agent 第一轮：从任务文本解析 draft_path，返回 read_file 调用。"""
    task = [m.content for m in messages if m.role == "user"][-1]
    draft_path = task.split("审阅草稿文件 ")[1].split("，")[0]
    return Message(role="assistant", content=None, tool_calls=[{
        "id": "cr", "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": draft_path})},
    }])


@pytest.mark.asyncio
async def test_generate_note_two_round_review_loop(agent_env, agent_registry):
    """两轮审稿循环：write_file 草稿 v1 → spawn 审稿 fail → edit_file 修订 → spawn 审稿 pass → 定稿。

    父 writer 两次 spawn_sub_agent(agent_type=reviewer, task="审阅草稿文件 …，
    对照原文 …")；reviewer 子 agent 读草稿后给 fail（缺实验结果）→ 父 edit_file 定向
    search-replace 插入章节 → 再审 pass。草稿自始至终在最终路径（vault note）：
    write_file 落 v1，修订走 edit_file 写回同一路径（不再 in-context 修订 + 另起 scratch）。"""
    cfg, _ = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    note_out = Path(cfg.vault_note_dir) / "paper.md"
    template = Path(cfg.workspace) / "templates" / "paper_note.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("# 标题\n## 概述\n## 方法\n## 实验结果\n", encoding="utf-8")

    task = f"审阅草稿文件 {note_out}，对照原文 {pdf}"
    mock = LoopMockLLM()
    # writer 轮次（静态）
    mock.add(_tc("read_file", {"path": str(template)}))
    mock.add(_tc("read_pdf", {"path": str(pdf)}))
    mock.add(_tc("write_file", {"path": str(note_out), "content": "# 标题\n## 概述\n## 方法\n"}))   # 草稿 v1
    mock.add(_tc("spawn_sub_agent", {"agent_type": "reviewer", "mode": "note_review", "task": task}))
    # 第 1 轮：子 agent 读草稿 → 裁决 fail（blocking: 缺实验结果）
    mock.add(child_read)
    mock.add(Message(role="assistant", content="审查裁决：fail\n- [BLOCKING] structure | 实验结果 | 补充实验结果章节"))
    # 修订：edit_file 定向插入"实验结果"节
    mock.add(_tc("edit_file", {"path": str(note_out),
                               "old_text": "## 方法\n",
                               "new_text": "## 方法\n## 实验结果\n"}))
    # 第 2 轮：修订后 → 子 agent 读草稿 → 裁决 pass
    mock.add(_tc("spawn_sub_agent", {"agent_type": "reviewer", "mode": "note_review", "task": task}))
    mock.add(child_read)
    mock.add(Message(role="assistant", content="审查裁决：pass"))
    # 定稿
    mock.add(Message(role="assistant", content="笔记已生成"))

    # make_agent：真实安全链 + confirm_callback 自动接受（write_file 定稿是用户门）
    agent = make_agent(agent_registry, "writer", mock, cfg)
    result = await agent.run(f"为 {pdf} 生成笔记")
    assert "笔记已生成" in result
    assert mock.callable_hits == 2                 # 子 agent 跑了两轮（审稿两次）
    # 父→子桥接 load-bearing：A-ii 下 draft_path 固定为 note_out（两轮同路径），
    # 无法再用"唯一任务文本数"断言 spawn 次数（set 去重后只剩 1）。
    # 注意 mock 按"每次 LLM 调用"追加 user 消息：同一子 run 的 task 文本会被追加两次
    # （首轮工具调用 + 次轮最终回答），故 sum ≥ 2 即证明两次 spawn 都发生。
    assert sum(1 for t in mock.seen_tasks if "审阅草稿文件" in t) >= 2
    assert note_out.exists()                       # 定稿落盘
    # 修订已生效：v1 无"实验结果"节，edit_file 定向 search-replace 插入后才出现——
    # startswith("# 标题") 在 v1 也成立，无法证明修订真正改写 note_out，故用更强断言。
    assert "## 实验结果" in note_out.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_generate_note_gives_up_after_three_rounds(agent_env, agent_registry):
    """blocking 3 轮未清零 → 停止循环（不再有第 4 次提交），返回路径 + 明示未达标。

    驱动：3 轮全部返回同一 blocking（缺实验结果），writer 每轮 edit_file
    尝试修订但裁决始终 fail；第 3 轮后 writer 应停止并明示未达标。
    callable_hits == 3 精确证明恰好 3 次 child spawn（= 3 次 spawn_sub_agent 提交）。"""
    cfg, _ = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    note_out = Path(cfg.vault_note_dir) / "paper.md"
    template = Path(cfg.workspace) / "templates" / "paper_note.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("# 标题\n## 概述\n## 方法\n", encoding="utf-8")

    task = f"审阅草稿文件 {note_out}，对照原文 {pdf}"
    mock = LoopMockLLM()
    mock.add(_tc("read_file", {"path": str(template)}))
    mock.add(_tc("read_pdf", {"path": str(pdf)}))
    mock.add(_tc("write_file", {"path": str(note_out), "content": "# 标题\n## 概述\n## 方法\n"}))
    for _ in range(3):
        mock.add(_tc("spawn_sub_agent", {"agent_type": "reviewer", "mode": "note_review", "task": task}))
        mock.add(child_read)
        mock.add(Message(role="assistant", content="审查裁决：fail\n- [BLOCKING] structure | 实验结果 | 补充实验结果章节"))
        mock.add(_tc("edit_file", {"path": str(note_out),
                                   "old_text": "## 方法\n",
                                   "new_text": "## 方法\n## 实验结果\n"}))
    # 第 3 轮 fail 后：停止循环，返回路径 + 明示未达标
    mock.add(Message(role="assistant", content="笔记已生成，仍有 blocking 意见未解决"))

    agent = make_agent(agent_registry, "writer", mock, cfg)
    result = await agent.run(f"为 {pdf} 生成笔记")
    assert "未解决" in result
    assert mock.callable_hits == 3          # 恰好 3 次提交，无第 4 次（若尝试第 4 次 mock pop 空 → IndexError）
    assert note_out.exists()


def test_writer_has_ask_user_question(agent_registry):
    """writer opt-in 装配 ask_user_question：格式/篇幅/语言偏好歧义时中途问用户。"""
    config = agent_registry.get_config("writer")
    names = {t.name for t in config.tools}
    assert "ask_user_question" in names


def test_writer_has_rag_retrieve(agent_registry):
    """writer 装配 rag_retrieve：大纲模式用 RAG 发现相关笔记、回溯论文段落。"""
    config = agent_registry.get_config("writer")
    names = {t.name for t in config.tools}
    assert "rag_retrieve" in names
