# tests/test_tools_file_extra.py
from pathlib import Path

from paperflow.tools import ReadPdfTool, MarkReadTool, FormatAnswerTool, FormatCheckTool, SuggestEditTool


def test_format_check_compares_template(tmp_path):
    # 构造模板 + 笔记
    tpl = tmp_path / "ws" / "templates" / "paper_note.md"
    tpl.parent.mkdir(parents=True)
    tpl.write_text("# 标题\n## 概述\n## 方法\n", encoding="utf-8")
    note = tmp_path / "note" / "n.md"
    note.parent.mkdir(parents=True)
    note.write_text("# 标题\n## 概述\n", encoding="utf-8")
    tool = FormatCheckTool()
    # 通过实例属性注入 workspace（测试用；生产经 get_rag_service().config 配置）
    tool._template_path = str(tpl)
    result = tool.execute(path=str(note))
    assert "方法" in result.text               # 缺失的模板章节被指出


def test_format_check_creates_skeleton_when_template_missing(tmp_path):
    # IMPORTANT-4 回归：spec §1 承诺"模板缺失时建最小骨架"。模板文件不存在时
    # execute 必须创建最小骨架再对比（生产默认 workspace="data" 无模板时不再
    # FileNotFoundError 中断 review-note 流程）。
    tpl = tmp_path / "ws" / "templates" / "paper_note.md"   # 故意不存在
    note = tmp_path / "note" / "n.md"
    note.parent.mkdir(parents=True)
    note.write_text("# 我的笔记\n## 概述\n", encoding="utf-8")
    tool = FormatCheckTool()
    tool._template_path = str(tpl)
    result = tool.execute(path=str(note))
    assert tpl.exists()                        # 骨架已落盘
    text = tpl.read_text(encoding="utf-8")
    assert "## 方法" in text and "## 实验结果" in text and "## 相关工作" in text
    assert "方法" in result.text               # 骨架章节参与对比并被指出


def test_format_answer_ok():
    tool = FormatAnswerTool()
    result = tool.execute(answer="这是回答")
    assert "这是回答" in result.text


def test_read_pdf_tool_meta():
    assert ReadPdfTool.allowed_roots == ["pdf"]
    assert ReadPdfTool.output_scan == "mark"


def test_mark_read_tool_meta():
    assert MarkReadTool.allowed_roots == ["pdf"]   # MINOR-6：对齐 spec §14
