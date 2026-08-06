# paperflow/tools/glob.py
"""GlobTool：按文件名模式在 vault 内定位文件（只读）。

generate-note 定位 PDF/笔记、search-paper 下载前去重、answer-question 找论文——
agent 不再盲猜精确路径（P2 路径风暴根因）。只读 → low、无确认。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult


class GlobTool(Tool):
    name = "glob"
    description = ("按 glob 模式列出文件路径（如 **/*.pdf、**/*Disentangled*.pdf）。"
                   "用于定位文件、检查文件是否已存在。root 指定搜索根（note/pdf/memory）。")
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式（** 递归）"},
            "root": {"type": "string", "format": "path",
                     "description": "搜索根目录（默认笔记目录；可传 pdf/memory 根）"},
        },
        "required": ["pattern"],
    }
    risk_level = "low"
    allowed_roots = ["note", "pdf", "memory"]

    def execute(self, pattern: str, root: str | None = None) -> ToolResult:
        # 通过 _config 取默认根（make_tools 注入）；root 显式传入则覆盖默认。
        # 保持 config 读取为防御式 getattr——测试与裸构造时可能没有 _config。
        cfg = getattr(self, "_config", None)
        base = Path(root) if root else Path(cfg.vault_note_dir)
        try:
            hits = [str(p) for p in base.glob(pattern)][:50]   # 封顶防爆炸
        except ValueError as e:                                # 非法模式（空等）
            return ToolResult(text=f"glob 模式无效: {e}")
        return ToolResult(text="\n".join(hits) if hits else "无匹配")
