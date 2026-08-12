# tests/agent/test_agent_writer_outline.py
"""writer 大纲模式流程测试：盘点→熔断→确认点→成稿→reviewer 审稿循环。"""
import json
from pathlib import Path

import pytest

from paperflow.core.llm import Message
from tests.conftest import make_mock_llm, _tc, make_agent
from tests.agent.test_agent_writer import LoopMockLLM


def _seed_notes_and_rag(svc, note_dir, reranker):
    """写 3 篇笔记文件并索引进 RAG（rag_retrieve 才能命中）。"""
    from paperflow.rag.parsers.chunker import Chunk
    notes = {
        "circRNA机制.md": "circRNA 作为 miRNA 海绵，在疾病中起调控作用。",
        "circRNA预测.md": "图神经网络方法用于 circRNA-疾病关联预测，效果优于传统方法。",
        "circRNA数据集.md": "公开数据集收集了 circRNA 与疾病的实验验证关联。",
    }
    chunks = []
    for name, text in notes.items():
        p = Path(note_dir) / name
        p.write_text(text, encoding="utf-8")
        chunks.append(Chunk(id=name, text=text, path=str(p),
                            source="note", heading="H", chunk_index=0))
    svc._reranker = reranker
    vecs = svc._embedder([c.text for c in chunks])
    svc._ensure_vector_store().upsert(chunks, vecs, mtime=1.0)
    svc._ensure_bm25().rebuild([(c.id, c.text) for c in chunks])
    return {name: Path(note_dir) / name for name in notes}


def child_outline_read(messages):
    """reviewer 大纲审阅子 agent 第一轮：从任务文本解析大纲路径，read_file。"""
    task = [m.content for m in messages if m.role == "user"][-1]
    outline_path = task.split("审阅大纲：")[1].split("。")[0]
    return Message(role="assistant", content=None, tool_calls=[{
        "id": "cr", "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": outline_path})},
    }])


@pytest.mark.asyncio
async def test_outline_happy_path(agent_env, agent_registry):
    """大纲 e2e：RAG 发现笔记→读笔记/模板→确认点→write 落盘→reviewer 审稿 pass→定稿。"""
    from paperflow.rag.encoders.reranker import FakeReranker
    cfg, svc = agent_env
    notes = _seed_notes_and_rag(svc, Path(cfg.vault_note_dir), FakeReranker())

    template = Path(cfg.workspace) / "templates" / "research_outline.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("# 研究大纲：<课题>\n## 1. 核心论点总览\n## 5. 断层/冗余/缺口标注\n",
                        encoding="utf-8")

    outline_out = Path(cfg.workspace) / "outline" / "circRNA关联预测.md"
    content = ("# 研究大纲：circRNA 关联预测\n"
               "## 1. 核心论点总览\n"
               "论点1 ← 笔记「circRNA机制」+ 论文「X」§3.2 [来源:论文「X」§3.2]\n")

    task = "任务模式：outline。课题：circRNA 关联预测。请梳理研究大纲。"
    outline_review_task = f"审阅大纲：{outline_out}。课题：circRNA 关联预测。相关笔记：[{list(notes.values())[0]}]"
    mock = LoopMockLLM()
    mock.add(_tc("rag_retrieve", {"query": "circRNA 关联预测"}))
    mock.add(_tc("read_file", {"path": str(list(notes.values())[0])}))
    mock.add(_tc("read_file", {"path": str(template)}))
    mock.add(_tc("ask_user_question", {"question": "我找到了相关笔记并提炼了核心论点候选，是否需要增删论点？或直接继续？"}))
    mock.add(_tc("write_file", {"path": str(outline_out), "content": content}))
    mock.add(_tc("spawn_sub_agent", {"agent_type": "reviewer", "mode": "outline_review",
                                     "task": outline_review_task}))
    mock.add(child_outline_read)
    mock.add(Message(role="assistant", content="审查裁决：pass"))
    mock.add(Message(role="assistant", content="大纲已生成"))

    agent = make_agent(agent_registry, "writer", mock, cfg)
    agent.system_prompt = f"当前模式：outline\n{agent.system_prompt}"
    result = await agent.run(task)
    assert "大纲已生成" in result
    assert outline_out.exists()


@pytest.mark.asyncio
async def test_outline_gives_up_when_no_material(agent_env, agent_registry):
    """素材熔断：RAG 无命中 → 不落盘，返回缺口方向。"""
    cfg, _ = agent_env
    task = "任务模式：outline。课题：某冷门方向。请梳理研究大纲。"
    mock = LoopMockLLM()
    mock.add(_tc("rag_retrieve", {"query": "某冷门方向"}))
    mock.add(Message(role="assistant",
                     content="当前笔记积累不足以支撑大纲，建议先精读以下方向：[缺口方向]"))
    agent = make_agent(agent_registry, "writer", mock, cfg)
    agent.system_prompt = f"当前模式：outline\n{agent.system_prompt}"
    result = await agent.run(task)
    assert "不足以支撑" in result
    outline_dir = Path(cfg.workspace) / "outline"
    assert not (outline_dir.exists() and list(outline_dir.glob("*.md")))


@pytest.mark.asyncio
async def test_outline_asks_topic_when_unknown(agent_env, agent_registry):
    """无课题 → ask_user_question 问研究方向再继续。"""
    cfg, _ = agent_env
    task = "任务模式：outline。请梳理研究大纲。"
    mock = LoopMockLLM()
    mock.add(_tc("ask_user_question", {"question": "请告诉我你想围绕哪个研究方向梳理大纲？"}))
    mock.add(Message(role="assistant", content="已了解课题方向，开始梳理"))
    agent = make_agent(agent_registry, "writer", mock, cfg)
    agent.system_prompt = f"当前模式：outline\n{agent.system_prompt}"
    result = await agent.run(task)
    assert "课题" in result or "方向" in result
