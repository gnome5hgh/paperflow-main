"""文件类原子 Tool：Read/Write/Edit/ReadPdf/MarkRead/FormatAnswer/FormatCheck/SuggestEdit。

安全边界靠中间件强制：format="path" → WorkspacePolicy（绝对路径 + allowed_roots 白名单）、
format="content" → SecurityScan（critical 硬阻断）、output_scan="mark" → 外部内容横幅。
Tool 自身不重复校验——声明元数据即可。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service

#: 可写的目录语义根（Note 可写；Paper=pdf 只读，SCOPE 硬边界）
_NOTE_ROOTS = ["note", "memory"]


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取笔记/论文 PDF 路径/记忆目录下的文本文件内容"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "文件绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    # 读面含 templates（LLM 读模板）+ scratch（子 agent 读落盘桥草稿）
    allowed_roots = ["note", "pdf", "memory", "templates", "scratch"]
    output_scan = "mark"                       # 外部文件内容 → SecurityScan 打未校验横幅
    side_effects = ["read_file"]

    def execute(self, path: str) -> ToolResult:
        return ToolResult(text=Path(path).read_text(encoding="utf-8"))


class WriteFileTool(Tool):
    name = "write_file"
    description = "写入新笔记（Note，非 Paper）到 note/ 或 memory/ 目录；已存在的文件将被拒绝（新建专用，修改请用 edit_file）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "目标文件绝对路径（不得已存在）"},
            "content": {"type": "string", "format": "content", "description": "文件内容"},
        },
        "required": ["path", "content"],
    }
    risk_level = "medium"                      # 写操作；新建可丢弃重来 → medium
    requires_confirm = True
    allowed_roots = _NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        # 存在性守卫：Write 只用于新建（medium 理由"新建可丢弃重来"只在真新建时成立）。
        # 覆盖既有文件必须走 edit_file（high，需会话确认）——否则 LLM 可用 write_file
        # 绕过 edit 的 high 风险闸门。这是风险语义（强制工具选择），非安全边界——
        # 真正边界是 WorkspacePolicy 的 allowed_roots。不做锁、不做二次检查（TOCTOU 可接受）。
        if p.exists():
            return ToolResult(text=f"文件已存在，请用 edit_file 修改（write_file 仅用于新建）: {path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        get_rag_service().index_document(str(p))   # 热更新钩子：会话内立即可检索
        return ToolResult(text=f"已写入 {path}")


class EditFileTool(Tool):
    name = "edit_file"
    description = "修改既有笔记内容（覆盖式编辑，破坏性强于新建）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "文件绝对路径"},
            "content": {"type": "string", "format": "content", "description": "新的完整内容"},
        },
        "required": ["path", "content"],
    }
    risk_level = "high"                        # 编辑修改既有内容 + 影响已索引文档 → high
    requires_confirm = True                    # 会话级确认；不放宽 max_risk
    allowed_roots = _NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        if not p.exists():
            return ToolResult(text=f"文件不存在: {path}")
        p.write_text(content, encoding="utf-8")
        get_rag_service().index_document(str(p))
        return ToolResult(text=f"已编辑 {path}")


class ReadPdfTool(Tool):
    name = "read_pdf"
    description = "解析 PDF 论文为结构化文本（GROBID，不可用时回退 PyMuPDF）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "PDF 绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    allowed_roots = ["pdf"]                    # Paper 只读
    output_scan = "mark"
    side_effects = ["read_file"]

    def execute(self, path: str) -> ToolResult:
        parser = get_rag_service().pdf_parser()
        doc = parser.parse_pdf(path)
        text = "\n\n".join(f"## {h}\n{t}" for h, t in doc.sections)
        return ToolResult(text=text or "（PDF 未能解析出文本）")


class MarkReadTool(Tool):
    """标记已读：只把 pdf 路径记入 history.jsonl，不读文件内容。

    pdf 根声明与只读边界一致；写入经 MemoryStore.append_history（已带 self._lock）。
    """

    name = "mark_read"
    description = "标记某篇论文/笔记为已读（记录到阅读历史）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "论文/笔记绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    allowed_roots = ["pdf"]                    # MINOR-6：对齐 spec §14，mark-read 针对论文（pdf 只读根）
    side_effects = ["write_file"]

    def execute(self, path: str) -> ToolResult:
        from paperflow.core.memory.experience_memory import MemoryStore
        from paperflow.config import PaperFlowConfig
        config = PaperFlowConfig.from_env()
        store = MemoryStore(Path(config.workspace) / "memory")
        store.append_history({"type": "mark_read", "path": path})
        return ToolResult(text=f"已标记已读: {path}")


class FormatAnswerTool(Tool):
    name = "format_answer"
    description = "格式化最终回答输出（内容安全扫描硬阻断由中间件执行）"
    parameters = {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "format": "content", "description": "回答文本"},
        },
        "required": ["answer"],
    }
    risk_level = "low"

    def execute(self, answer: str) -> ToolResult:
        return ToolResult(text=f"## 回答\n\n{answer}")


class FormatCheckTool(Tool):
    """笔记 Markdown 标题树 vs 模板对比（确定性代码，供 review-note 用）。"""

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
    # 也读草稿文件（execute(path) 从磁盘读结构）→ 需要 scratch；templates 由内部派生不在此
    allowed_roots = ["note", "scratch"]

    #: 模板缺失时落盘的最小骨架（spec §1/§14：缺失时建最小骨架而非报错）
    _SKELETON = ("# <论文标题>\n"
                 "## 概述\n"
                 "## 方法\n"
                 "## 实验结果\n"
                 "## 相关工作\n"
                 "## 局限与展望\n")

    def __init__(self):
        super().__init__()
        # 模板是工具内部常量路径（非 LLM 可指定的 path 参数），不进 allowed_roots 映射；
        # 默认按 <workspace>/templates/paper_note.md；测试可注入 _template_path
        self._template_path = None

    def _ensure_template(self) -> list[str]:
        """读取模板标题树；文件不存在时先建最小骨架再读。

        IMPORTANT-4 修复：spec §1 承诺"缺失时建最小骨架"，原 _load_template
        直接 read_text 会在默认 workspace="data" 且模板未生成时 FileNotFoundError
        中断 review-note 流程。先确保模板存在（生产路径不再抛错），再提取标题。"""
        cfg = get_rag_service().config
        # cfg.workspace 是 str（config.py:67），需先包 Path 才能用 / 拼接；
        # 模板是内部常量路径，不进 allowed_roots 映射（WorkspacePolicy 不校验）
        tpl = Path(self._template_path or (Path(cfg.workspace) / "templates" / "paper_note.md"))
        if not tpl.exists():
            tpl.parent.mkdir(parents=True, exist_ok=True)
            tpl.write_text(self._SKELETON, encoding="utf-8")
        return [ln.lstrip("# ").strip() for ln in tpl.read_text(encoding="utf-8").splitlines()
                if ln.startswith("#")]

    def execute(self, path: str) -> ToolResult:
        note_heads = [ln.lstrip("# ").strip()
                      for ln in Path(path).read_text(encoding="utf-8").splitlines()
                      if ln.startswith("#")]
        missing = [h for h in self._ensure_template() if h not in note_heads]
        if missing:
            return ToolResult(text=f"缺少模板章节: {', '.join(missing)}")
        return ToolResult(text="结构完整，与模板一致")


class SuggestEditTool(Tool):
    name = "suggest_edit"
    description = "汇总对一篇笔记的修改建议（供 review-note 返回）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "笔记绝对路径"},
            "suggestions": {"type": "array", "items": {"type": "string"},
                            "description": "修改建议列表"},
        },
        "required": ["path", "suggestions"],
    }
    risk_level = "low"
    # 审稿流目标是 scratch 草稿路径（review-note 对草稿给建议），与 FormatCheckTool 同根；
    # 不加 scratch 时真实 WorkspacePolicy 会拦截草稿路径（draft 在 workspace/tmp），
    # 且 execute 不读文件内容（只把 suggestions 按 path 标签格式化），放开零安全影响。
    allowed_roots = ["note", "scratch"]

    def execute(self, path: str, suggestions: list[str]) -> ToolResult:
        lines = "\n".join(f"- {s}" for s in suggestions)
        return ToolResult(text=f"对 {path} 的建议：\n{lines}")
