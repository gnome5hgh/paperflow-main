"""FormatCheckTool：笔记 Markdown 标题树与模板对比(review-agent 用,确定性代码)。

模板取自 **review-note skill** 随流程分发的资源——审查方的标准就是它手里那份副本，
与 review-note 流程里 load_skill 读到的是同一个文件。刻意不读 write-note 那份：
工具按 A 判定、审查方按 B 判定时，同一篇笔记会同时得到「结构完整」和「缺章节」，
这比模板缺失更难查。

路径经 `SkillRegistry` 解析成绝对路径（工具声明 needs_skill_registry，构造时注入）：
注册表在启动时把 skills 根定死，工具不再每次调用自己按工作目录拼相对路径——那是
对同一个根的第二次解析，两次错位就是静默取错模板。模板缺失时**返回明确错误、绝不落盘**：
落盘会往已安装的 skill 目录里写文件。
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
        needs_skill_registry: bool，True——模板路径由 SkillRegistry 解析（不自行拼路径）
        _template_path: str | None，模板路径覆盖（测试注入用；设了就不再走注册表）
        _skill_registry: SkillRegistry | None，运行时注入的 skill 注册表
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

    #: 模板在 skill 体系里的坐标（模板随审查流程分发，审查方读同一份）
    _SKILL = "review-note"
    _AGENT_TYPE = "review-agent"
    _RESOURCE = "references/paper_note.md"

    #: 声明需要 skill 注册表：运行时构造期注入（与 needs_parent 同款 opt-in）
    needs_skill_registry = True

    def __init__(self):
        """初始化工具；模板路径缺省经 skill 注册表解析（测试可注入 _template_path）。"""
        super().__init__()
        self._template_path: str | None = None
        self._skill_registry = None

    def attach_skill_registry(self, registry) -> None:
        """注入 skill 注册表（运行时构造期调用，见 Agent.__init__ 的 opt-in 注入）。

        Args:
            registry: SkillRegistry，用于把模板坐标解析成绝对路径。
        """
        self._skill_registry = registry

    def _template_file(self) -> Path | None:
        """定位模板文件；测试注入优先，其次经注册表解析，都拿不到则返回 None。"""
        if self._template_path:
            return Path(self._template_path)
        if self._skill_registry is None:
            return None
        try:
            return self._skill_registry.resource_path(self._SKILL, self._AGENT_TYPE, self._RESOURCE)
        except (KeyError, ValueError, FileNotFoundError):
            # skill 未安装 / 被停用 / 资源缺失：统一当作「模板不存在」，由 execute 报明确错误
            return None

    def _template_headings(self) -> list[str]:
        """读模板的标题树；模板不存在时返回空列表，由调用方转成明确错误（不落盘）。"""
        tpl = self._template_file()
        if tpl is None or not tpl.exists():
            return []
        return [ln.lstrip("# ").strip()
                for ln in tpl.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]

    def execute(self, path: str) -> ToolResult:
        """对比笔记标题树与模板,返回缺失章节清单或"结构完整"。

        Args:
            path: str，待检查笔记的绝对路径

        Returns:
            ToolResult，文本为缺失章节清单或「结构完整」；模板取不到时返回错误文本
            （不落盘、不中断调用方流程）。
        """
        headings = self._template_headings()
        if not headings:
            where = (self._template_path
                     or f"{self._SKILL} 的 {self._RESOURCE}（随审查流程分发）")
            return ToolResult(is_error=True, text=f"模板不存在，无法比对结构：{where}")
        note_heads = [ln.lstrip("# ").strip()
                      for ln in Path(path).read_text(encoding="utf-8").splitlines()
                      if ln.startswith("#")]
        missing = [h for h in headings if h not in note_heads]
        if missing:
            return ToolResult(text=f"缺少模板章节: {', '.join(missing)}")
        return ToolResult(text="结构完整，与模板一致")
