"""FormatCheckTool：笔记 Markdown 标题树与模板对比(reviewer 用,确定性代码)。

模板是工具内部常量路径(非 LLM 可指定的 path 参数),不进 allowed_roots 映射;
模板缺失时自动落盘最小骨架再对比,不抛错中断流程。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service


class FormatCheckTool(Tool):
    """笔记 Markdown 标题树与模板对比(确定性代码,供 reviewer 用)。"""

    name = "format_check"
    description = "检查笔记结构是否符合模板（对比 Markdown 标题树）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "笔记绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    # 也读草稿文件(execute 从磁盘读结构)→ 需要 scratch;templates 由内部派生不在此
    allowed_roots = ["note", "scratch"]

    #: 模板缺失时落盘的最小骨架(缺失建骨架而非报错)
    _SKELETON = ("# <论文标题>\n"
                 "## 概述\n"
                 "## 方法\n"
                 "## 实验结果\n"
                 "## 相关工作\n"
                 "## 局限与展望\n")

    def __init__(self):
        super().__init__()
        # 模板是内部常量路径,默认 <workspace>/templates/paper_note.md;测试可注入 _template_path
        self._template_path = None

    def _ensure_template(self) -> list[str]:
        """读取模板标题树;模板文件不存在时先落盘最小骨架再读。

        直接读不存在的模板会抛 FileNotFoundError 中断 reviewer 流程,故先确保模板
        存在(生产路径不再抛错),再提取各级标题。"""
        cfg = get_rag_service().config
        tpl = Path(self._template_path or (Path(cfg.workspace) / "templates" / "paper_note.md"))
        if not tpl.exists():
            tpl.parent.mkdir(parents=True, exist_ok=True)
            tpl.write_text(self._SKELETON, encoding="utf-8")
        return [ln.lstrip("# ").strip() for ln in tpl.read_text(encoding="utf-8").splitlines()
                if ln.startswith("#")]

    def execute(self, path: str) -> ToolResult:
        """对比笔记标题树与模板,返回缺失章节清单或"结构完整"。"""
        note_heads = [ln.lstrip("# ").strip()
                      for ln in Path(path).read_text(encoding="utf-8").splitlines()
                      if ln.startswith("#")]
        missing = [h for h in self._ensure_template() if h not in note_heads]
        if missing:
            return ToolResult(text=f"缺少模板章节: {', '.join(missing)}")
        return ToolResult(text="结构完整，与模板一致")
