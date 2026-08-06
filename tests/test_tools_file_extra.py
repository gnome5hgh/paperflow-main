# tests/test_tools_file_extra.py
from pathlib import Path

import pytest

from paperflow.tools import ReadPdfTool, MarkReadTool, FormatAnswerTool, FormatCheckTool


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
    """G：多候选 → raise（原返回"不唯一"错误文本——审计 success 误导，见分析 P4）。"""
    cfg, _ = agent_env
    _make_pdf(cfg, sub="Heterogeneous graph")
    _make_pdf(cfg, sub="Heterogeneous graph copy")
    requested = str(Path(cfg.vault_pdf_dir) / "Heterogeneous graph" / "Variational Disentangled Graph Auto-Encoders for Link Prediction.pdf")
    with pytest.raises(Exception) as ei:
        ReadPdfTool().execute(path=requested)
    assert "不唯一" in str(ei.value)


def test_read_pdf_fuzzy_no_match_reports_not_found(agent_env):
    """G：0 候选 → raise（原返回"未找到"错误文本）。"""
    cfg, _ = agent_env
    requested = str(Path(cfg.vault_pdf_dir) / "No Such Paper.pdf")
    with pytest.raises(Exception) as ei:
        ReadPdfTool().execute(path=requested)
    assert "未找到" in str(ei.value)


# ---- GlobTool / GrepTool：只读搜索工具（Task 2）----
# 定位/去重/锚点确认依赖 glob 命中与 grep 行级命中，治 P2 路径风暴。


def test_glob_finds_files(tmp_path):
    from paperflow.tools.glob import GlobTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note" / "sub").mkdir(parents=True)
    (tmp_path / "note" / "a.md").write_text("x")
    (tmp_path / "note" / "sub" / "b.md").write_text("y")
    tool = GlobTool()
    tool._config = cfg
    result = tool.execute(pattern="**/*.md")
    assert "a.md" in result.text and "b.md" in result.text
    result2 = tool.execute(pattern="*.md")     # 非递归
    assert "a.md" in result2.text and "sub" not in result2.text
    assert tool.risk_level == "low" and tool.allowed_roots == ["note", "pdf", "memory"]


def test_glob_no_match(tmp_path):
    from paperflow.tools.glob import GlobTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    tool = GlobTool(); tool._config = cfg
    assert "无匹配" in tool.execute(pattern="**/*.pdf").text


def test_glob_blocks_path_escape(tmp_path):
    # Important 1 回归：glob 不约束 pattern 到 base，`../../**/*.txt` 能命中 base 外
    # 路径（只读泄露，违反 allowed_roots 边界）。修复：逃逸命中（resolve 后不在 base
    # 内）逐个跳过——`p.relative_to(base)` 是纯词法比较，把 `..` 当普通路径段，拦不住。
    from paperflow.tools.glob import GlobTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    (tmp_path / "outside").mkdir(parents=True)
    (tmp_path / "note" / "inside.txt").write_text("in")
    (tmp_path / "outside" / "secret.txt").write_text("secret")
    tool = GlobTool(); tool._config = cfg
    result = tool.execute(pattern="../../**/*.txt")
    assert "secret.txt" not in result.text    # base 外路径被跳过
    assert "inside.txt" in result.text        # base 内文件正常返回


def test_glob_caps_at_50(tmp_path):
    # 封顶回归：命中 60 条时只返回 50 条（遍历即 break，不物化全部）。
    from paperflow.tools.glob import GlobTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    for i in range(60):
        (tmp_path / "note" / f"f{i:02d}.md").write_text("x")
    tool = GlobTool(); tool._config = cfg
    result = tool.execute(pattern="**/*.md")
    assert len(result.text.splitlines()) == 50


def test_grep_finds_lines(tmp_path):
    from paperflow.tools.grep import GrepTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    (tmp_path / "note" / "a.md").write_text("line1\nDPNS method\nline3", encoding="utf-8")
    tool = GrepTool(); tool._config = cfg
    result = tool.execute(pattern="DPNS", path=str(tmp_path / "note"))
    assert "a.md:2:" in result.text and "DPNS" in result.text
    assert tool.risk_level == "low"


def test_grep_miss_and_bad_regex(tmp_path):
    from paperflow.tools.grep import GrepTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    (tmp_path / "note" / "a.md").write_text("hello", encoding="utf-8")
    tool = GrepTool(); tool._config = cfg
    assert "无匹配" in tool.execute(pattern="zzz", path=str(tmp_path / "note")).text
    assert "正则无效" in tool.execute(pattern="[", path=str(tmp_path / "note")).text


def test_grep_returns_raw_line_for_anchor(tmp_path):
    # Important 4 回归：grep 命中行被当作 edit_file 的 old_text 锚点，必须原样返回
    # （不 strip、不截断）——缩进/超长行若被裁剪，模型复制过去就 miss，又变试错。
    from paperflow.tools.grep import GrepTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    indented = "    ## 方法\n"          # 前导缩进
    long_line = "## 摘要 " + "x" * 300 + "\n"  # 超长行
    (tmp_path / "note" / "a.md").write_text(indented + long_line, encoding="utf-8")
    tool = GrepTool(); tool._config = cfg
    r1 = tool.execute(pattern="## 方法", path=str(tmp_path / "note"))
    assert "    ## 方法" in r1.text      # 缩进原样保留
    r2 = tool.execute(pattern="x{300}", path=str(tmp_path / "note"))
    assert ("x" * 300) in r2.text        # 超长行不截断


def test_grep_caps_at_30(tmp_path):
    # 封顶回归：40 条命中只返回 30 条（原样行 + 封顶控制量）。
    from paperflow.tools.grep import GrepTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    (tmp_path / "note" / "a.md").write_text("match\n" * 40, encoding="utf-8")
    tool = GrepTool(); tool._config = cfg
    result = tool.execute(pattern="match", path=str(tmp_path / "note"))
    assert len(result.text.splitlines()) == 30


# ---- Write/Edit 语义（Task 3）：write=新建+覆盖，edit=定向 search-replace ----
# 2026-08-06 死锁修复：write_file 曾"仅新建"，已存在文件必须走 edit_file，但 edit_file
# 又因 high>medium 被默认拒绝 → 已存在文件无任何可写工具。现两者同 medium：write 整篇
# 写/重写，edit 定向替换。


def test_write_file_overwrites_existing(tmp_path):
    from paperflow.tools.write_file import WriteFileTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    f = tmp_path / "note" / "a.md"
    f.write_text("旧内容", encoding="utf-8")
    tool = WriteFileTool(); tool._config = cfg
    result = tool.execute(path=str(f), content="新内容")
    assert f.read_text(encoding="utf-8") == "新内容"     # 覆盖成功
    assert "已写入" in result.text


def test_edit_file_search_replace(tmp_path):
    from paperflow.tools.edit_file import EditFileTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    f = tmp_path / "note" / "a.md"
    f.write_text("# 标题\n## 方法\n", encoding="utf-8")
    tool = EditFileTool(); tool._config = cfg
    result = tool.execute(path=str(f), old_text="## 方法\n", new_text="## 方法\n## 实验结果\n")
    assert "## 实验结果" in f.read_text(encoding="utf-8")
    assert "已编辑" in result.text
    assert tool.risk_level == "medium"     # 不再 high → 默认会话可确认


def test_edit_file_miss_and_multi(tmp_path):
    from paperflow.tools.edit_file import EditFileTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    f = tmp_path / "note" / "a.md"
    f.write_text("abc", encoding="utf-8")
    tool = EditFileTool(); tool._config = cfg
    assert "未找到" in tool.execute(path=str(f), old_text="zzz", new_text="x").text
    f.write_text("abcabc", encoding="utf-8")
    assert "次" in tool.execute(path=str(f), old_text="abc", new_text="x").text  # 多命中


def test_edit_file_missing_file(tmp_path):
    from paperflow.tools.edit_file import EditFileTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    tool = EditFileTool(); tool._config = cfg
    assert "文件不存在" in tool.execute(
        path=str(tmp_path / "note" / "nope.md"), old_text="a", new_text="b").text


def test_edit_file_empty_old_text(tmp_path):
    # Minor 10 回归：空 old_text 会走 str.count("") 的"多命中"分支（困惑的报错）。
    # 守卫直接明示参数错误，且文件不被改动。
    from paperflow.tools.edit_file import EditFileTool
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"),
                          vault_note_dir=str(tmp_path / "note"))
    (tmp_path / "note").mkdir(parents=True)
    f = tmp_path / "note" / "a.md"
    f.write_text("abc", encoding="utf-8")
    tool = EditFileTool(); tool._config = cfg
    r = tool.execute(path=str(f), old_text="", new_text="x")
    assert "old_text 不能为空" in r.text
    assert f.read_text(encoding="utf-8") == "abc"   # 未改动
