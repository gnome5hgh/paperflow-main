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


def test_read_pdf_routes_through_parse_pdf_cached(agent_env):
    """缓存入口路由：ReadPdfTool 走 parse_pdf_cached（而非裸 pdf_parser().parse_pdf）。

    agent_env fixture 已把 paperflow.tools.read_pdf.get_rag_service patch 成 svc
    （含 StubPdfParser）；断言 execute 确实经缓存入口——若改回 pdf_parser() 直调，
    此测试失败（called 为空）。"""
    from pathlib import Path
    cfg, svc = agent_env
    pdf = Path(cfg.vault_pdf_dir) / "p.pdf"
    pdf.write_bytes(b"dummy")
    called = []
    original = svc.parse_pdf_cached
    def spy(path):
        called.append(path)
        return original(path)
    svc.parse_pdf_cached = spy
    result = ReadPdfTool().execute(path=str(pdf))
    assert called
    assert "Abstract text." in result.text      # StubPdfParser 的 sections 内容


def _make_pdf(cfg, sub="Heterogeneous graph", name="Variational Disentangled Graph Auto-Encoders  for Link Prediction.pdf"):
    d = Path(cfg.vault_pdf_dir) / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"dummy")
    return p


def test_read_pdf_exact_path_preferred(agent_env):
    """exact 命中走原逻辑（parse 一次），不触发模糊分支。"""
    cfg, svc = agent_env
    p = _make_pdf(cfg)
    calls = []
    original = svc.parse_pdf_cached
    def spy(path):
        calls.append(path)
        return original(path)
    svc.parse_pdf_cached = spy
    result = ReadPdfTool().execute(path=str(p))
    assert calls == [str(p)]                    # 精确路径一次调用
    assert "Abstract text." in result.text


def test_read_pdf_fuzzy_matches_single_space_to_double_space(agent_env):
    """RC2b 回归：请求单空格路径（LLM 折叠后），实际文件双空格 → 归一化唯一命中。"""
    cfg, _ = agent_env
    p = _make_pdf(cfg)
    requested = str(p).replace("Auto-Encoders  for", "Auto-Encoders for")
    result = ReadPdfTool().execute(path=requested)
    assert "Abstract text." in result.text


def test_read_pdf_fuzzy_ambiguous_errors(agent_env):
    """D4 安全语义：归一化后多候选 → 明确错误（不猜），0 候选同样报错。"""
    cfg, _ = agent_env
    _make_pdf(cfg, sub="Heterogeneous graph")
    _make_pdf(cfg, sub="Heterogeneous graph copy")
    requested = str(Path(cfg.vault_pdf_dir) / "Heterogeneous graph" / "Variational Disentangled Graph Auto-Encoders for Link Prediction.pdf")
    result = ReadPdfTool().execute(path=requested)
    assert "不唯一" in result.text


def test_read_pdf_fuzzy_no_match_reports_not_found(agent_env):
    """D4 0 候选分支直接断言：pdf root 无匹配 → 明确"未找到"（不猜）。"""
    cfg, _ = agent_env
    requested = str(Path(cfg.vault_pdf_dir) / "No Such Paper.pdf")
    result = ReadPdfTool().execute(path=requested)
    assert "未找到" in result.text
