# paperflow/tools/file/glob.py
"""GlobTool：按文件名模式在资料库内定位文件(只读)。

note-agent 定位 PDF/笔记、paper-agent 下载前去重与找论文——让 agent 不必盲猜
精确路径。只读 → low 风险、无需确认。
"""
from pathlib import Path

from paperflow.core.security.middleware.workspace import is_denied_path
from paperflow.core.tool import Tool, ToolResult


class GlobTool(Tool):
    """按 glob 模式在语料库内定位文件的只读工具。

    Attributes:
        name: str，工具名 "glob"
        description: str，工具描述（含 root 说明）
        parameters: dict，JSON Schema（pattern/root）
        risk_level: str，"low"（只读）
        root_hints: list[str]，["note", "pdf", "memory"]
    """
    name = "glob"
    description = ("按 glob 模式列出文件路径（如 **/*.pdf、**/*Disentangled*.pdf）。"
                   "用于定位文件、检查文件是否已存在。root 指定搜索根（缺省=语料库笔记根，可传任意绝对路径）。")
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式（** 递归）"},
            "root": {"type": "string", "format": "path",
                     "description": "搜索根目录绝对路径（默认笔记目录）"},
        },
        "required": ["pattern"],
    }
    risk_level = "low"
    root_hints = ["note", "pdf", "memory"]

    def execute(self, pattern: str, root: str | None = None) -> ToolResult:
        """按 glob 模式列出匹配的文件路径(最多 50 条);根目录可显式指定。

        Args:
            pattern: glob 模式(** 递归匹配子目录)
            root: 搜索根目录绝对路径;缺省用配置的笔记目录,可传任意绝对路径

        Returns:
            命中路径每行一条;无匹配返回"无匹配"

        """
        # 通过 _config 取默认根(make_tools 注入);root 显式传入则覆盖默认。
        # config 读取用防御式 getattr——测试与裸构造时可能没有 _config。
        cfg = getattr(self, "_config", None)
        base = Path(root) if root else Path(cfg.corpus.note_dir if cfg else ".")
        try:
            # 白名单退役：读路径放开后无根约束，pattern 逃逸出 base 不再视为越界
            # （`../../**/*` 等命中照常返回），仅黑名单过滤防通配符枚举敏感路径。
            hits: list[str] = []
            for p in base.glob(pattern):
                # 敏感路径黑名单：命中 workspace/audit、.git 等直接跳过（防通配符枚举）
                if cfg is not None and is_denied_path(p.resolve(), cfg.runtime.workspace):
                    continue
                hits.append(str(p))
                if len(hits) >= 50:                          # 封顶防爆炸
                    break                                    # 遍历即截断，替代先物化后切片
        except ValueError as e:                              # 非法模式（空等）
            return ToolResult(text=f"glob 模式无效: {e}")
        return ToolResult(text="\n".join(hits) if hits else "无匹配")
