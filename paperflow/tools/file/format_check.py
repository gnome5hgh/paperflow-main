"""FormatCheckTool：笔记 Markdown 标题树与模板对比(review-agent 用,确定性代码)。

模板随写作流程进了 skill（`.paperflow/skills/write-note/references/paper_note.md`），
本工具从那里解析——它是确定性工具、不走 `load_skill`，所以这是一处代码级路径依赖。
模板缺失时**返回明确错误、绝不落盘**：落盘会往已安装的 skill 目录里写文件。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult


class FormatCheckTool(Tool):
    """笔记 Markdown 标题树与模板对比(确定性代码,供 review-agent 用)。

    Attributes:
        name: str，工具名 "format_check"
        description: str，工具描述
        parameters: dict，JSON Schema（path）
        risk_level: str，"low"
        root_hints: list[str]，["note", "scratch"]
        _template_path: str | None，模板路径覆盖（缺省取 write-note skill 的资源；测试注入用）
    """

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
    root_hints = ["note", "scratch"]
    #: 两侧都只读：读笔记与读模板比结构，不写任何落点
    side_effects = ["read_file"]

    #: 模板的默认位置：随 write-note skill 一起分发的资源（与 SkillRegistry 同锚 .paperflow/skills/，
    #: 即相对启动目录解析——不是配置派生，改动时须与 skill 扫描保持一致）
    _DEFAULT_TEMPLATE = Path(".paperflow/skills/write-note/references/paper_note.md")

    def __init__(self):
        """初始化工具；模板路径缺省按 skill 资源解析（测试可注入 _template_path）。"""
        super().__init__()
        self._template_path = None

    def _template_headings(self) -> list[str]:
        """读模板的标题树；模板不存在时返回空列表，由调用方转成明确错误（不落盘）。"""
        tpl = Path(self._template_path) if self._template_path else self._DEFAULT_TEMPLATE
        if not tpl.exists():
            return []
        return [ln.lstrip("# ").strip()
                for ln in tpl.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]

    def execute(self, path: str) -> ToolResult:
        """对比笔记标题树与模板,返回缺失章节清单或"结构完整"。

        Args:
            path: str，待检查笔记的绝对路径

        Returns:
            ToolResult，文本为缺失章节清单或「结构完整」；模板缺失时返回错误文本
            （不落盘、不中断调用方流程）。
        """
        headings = self._template_headings()
        if not headings:
            tpl = self._template_path or str(self._DEFAULT_TEMPLATE)
            return ToolResult(
                is_error=True,
                text=f"模板不存在，无法比对结构：{tpl}（模板随 write-note skill 分发）")
        note_heads = [ln.lstrip("# ").strip()
                      for ln in Path(path).read_text(encoding="utf-8").splitlines()
                      if ln.startswith("#")]
        missing = [h for h in headings if h not in note_heads]
        if missing:
            return ToolResult(text=f"缺少模板章节: {', '.join(missing)}")
        return ToolResult(text="结构完整，与模板一致")
