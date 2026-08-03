"""generate-note agent + ReviewDraftTool 测试。"""
import asyncio
from pathlib import Path

import pytest

from paperflow.core.llm import Message
from tests.conftest import make_mock_llm, _tc, make_agent


@pytest.mark.asyncio
async def test_review_draft_runs_child_and_cleans_scratch(agent_env, agent_registry):
    cfg, _ = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    # mock LLM：子 review-note 首轮即回最终意见（不调工具）——最简验证桥接与清理
    llm = make_mock_llm([
        Message(role="assistant", content="审稿意见：结构完整"),
    ])
    agent = make_agent(agent_registry, "generate-note", llm, cfg)
    tool = agent.tools["review_draft"]
    assert tool.needs_parent is True

    # 模拟生产：execute 经 asyncio.to_thread 跑在 worker 线程（无 running loop）
    result = await asyncio.to_thread(
        tool.execute,
        draft_text="# 标题\n## 概述\n## 方法\n",
        pdf_path=str(pdf),
    )
    assert "审稿意见" in result.text          # 子 agent 跑过（文本来自 mock 的 child 轮）
    # scratch 清理：workspace/tmp 下无残留
    scratch = Path(cfg.workspace) / "tmp"
    assert not list(scratch.glob("review_*.md"))


def test_generate_note_tools_metadata(agent_env, agent_registry):
    """generate-note 完整装配：6 工具齐备，ReviewDraftTool 声明 needs_parent。

    review_draft 是唯一需要 parent 注入的工具（嵌套 spawn 子 agent），
    其余原子工具（read/write/pdf）不需要——权限最小化。"""
    config = agent_registry.get_config("generate-note")
    names = [t.name for t in config.tools]
    assert "review_draft" in names
    assert "read_file" in names and "write_file" in names
    review = next(t for t in config.tools if t.name == "review_draft")
    assert review.needs_parent is True


@pytest.mark.asyncio
async def test_generate_note_single_round(agent_env, agent_registry):
    """单轮 happy path：读模板 → 读 PDF → 审稿（一次即通过）→ write_file 定稿落盘。

    mock 序列与真实 ReAct 对齐：审稿子 agent（review-note）首轮即回最终意见
    （"通过"），父 generate-note 不再循环直接定稿。make_agent 接真实安全链，
    write_file requires_confirm=True → confirm_callback 自动接受（spec §4.1
    定稿是用户门，此处测试侧代为通过），落盘触发 index_document（patch 的
    svc + FakeEmbedder，不建真实 RAG 栈）。"""
    cfg, _ = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    note_out = Path(cfg.vault_note_dir) / "paper.md"
    template = Path(cfg.workspace) / "templates" / "paper_note.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("# 标题\n## 概述\n## 方法\n", encoding="utf-8")

    llm = make_mock_llm([
        _tc("read_file", {"path": str(template)}),
        _tc("read_pdf", {"path": str(pdf)}),
        _tc("review_draft", {"draft_text": "# 标题\n## 概述\n## 方法\n", "pdf_path": str(pdf)}),
        Message(role="assistant", content="审稿意见：结构完整，通过"),   # 子 agent 最终意见
        _tc("write_file", {"path": str(note_out), "content": "# 标题\n## 概述\n## 方法\n"}),
        Message(role="assistant", content="笔记已生成"),
    ])
    agent = make_agent(agent_registry, "generate-note", llm, cfg)
    result = await agent.run(f"为 {pdf} 生成笔记")
    assert "笔记已生成" in result
    assert note_out.exists()              # 定稿落盘（confirm_callback 已接受 write_file）


import json
from paperflow.core.llm import Message
from tests.conftest import make_agent, _tc  # LoopMockLLM 本文件自定义


class LoopMockLLM:
    """e2e 专用 mock：预设响应序列；callable 项接收 messages 动态构造。

    子 agent 的 read_file 轮次需要 draft_path（uuid 文件名测试无法预知），
    只能从子 agent 的 user 任务文本（"审阅草稿文件 <draft>，对照原文 <pdf>"）解析。"""

    def __init__(self):
        self.responses = []
        self.seen_tasks = []
        self.callable_hits = 0

    def add(self, resp):
        self.responses.append(resp)

    async def chat(self, messages, tools=None, tool_choice="auto"):
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
    cfg, _ = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "paper.pdf"
    pdf.write_bytes(b"dummy")
    note_out = Path(cfg.vault_note_dir) / "paper.md"
    template = Path(cfg.workspace) / "templates" / "paper_note.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("# 标题\n## 概述\n## 方法\n## 实验结果\n", encoding="utf-8")

    mock = LoopMockLLM()
    # generate-note 轮次（静态）
    mock.add(_tc("read_file", {"path": str(template)}))
    mock.add(_tc("read_pdf", {"path": str(pdf)}))
    mock.add(_tc("review_draft", {"draft_text": "# 标题\n## 概述\n## 方法\n", "pdf_path": str(pdf)}))
    # 审稿循环 第 1 轮：子 agent 读草稿 → 意见"缺实验结果"（驱动 in-context 修订）
    mock.add(child_read)
    mock.add(Message(role="assistant", content="草稿缺实验结果，请补充"))
    # 第 2 轮：修订后草稿 → 子 agent 读草稿 → 意见"通过"
    mock.add(_tc("review_draft", {"draft_text": "# 标题\n## 概述\n## 方法\n## 实验结果\n", "pdf_path": str(pdf)}))
    mock.add(child_read)
    mock.add(Message(role="assistant", content="结构完整，通过"))
    # 定稿
    mock.add(_tc("write_file", {"path": str(note_out), "content": "# 标题\n## 概述\n## 方法\n## 实验结果\n"}))
    mock.add(Message(role="assistant", content="笔记已生成"))

    # make_agent：真实安全链 + confirm_callback 自动接受（write_file 定稿是用户门）
    agent = make_agent(agent_registry, "generate-note", mock, cfg)
    result = await agent.run(f"为 {pdf} 生成笔记")
    assert "笔记已生成" in result
    assert mock.callable_hits == 2                 # 子 agent 跑了两轮（审稿两次）
    assert note_out.exists()                       # 定稿落盘
    assert note_out.read_text(encoding="utf-8").startswith("# 标题")
    # scratch 清理：两轮审稿后 workspace/tmp 无残留
    assert not list((Path(cfg.workspace) / "tmp").glob("review_*.md"))
