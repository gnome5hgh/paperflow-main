# paperflow/tools/glob.py
"""GlobTool：按文件名模式在 vault 内定位文件（只读）。

generate-note 定位 PDF/笔记、search-paper 下载前去重、answer-question 找论文——
agent 不再盲猜精确路径（P2 路径风暴根因）。只读 → low、无确认。
"""
from pathlib import Path

from paperflow.core.security.workspace import is_denied_path
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
        base = Path(root) if root else Path(cfg.vault_note_dir if cfg else ".")
        try:
            # 越界防护（Important 1）：glob 不约束 pattern 到 base，`../../**/*` 能命中
            # base 外路径（只读泄露，违反 allowed_roots 边界）。逐个过滤命中：
            # 逃逸（resolve 后不在 base 内）→ 跳过。
            # 注意必须用 resolve() 比较——`p.relative_to(base)` 是纯词法比较，把 `..`
            # 当作普通路径段，`base/../../outside/f` 不会触发 ValueError，根本拦不住逃逸。
            base_resolved = base.resolve()
            hits: list[str] = []
            for p in base.glob(pattern):
                try:
                    p.resolve().relative_to(base_resolved)  # 逃逸(base 外)→ 跳过
                except ValueError:
                    continue
                # 敏感路径黑名单：base 内含 workspace/audit 等 → 跳过（防通配符枚举）
                if cfg is not None and is_denied_path(p.resolve(), cfg.workspace):
                    continue
                hits.append(str(p))
                if len(hits) >= 50:                          # 封顶防爆炸
                    break                                    # 遍历即截断，替代先物化后切片
        except ValueError as e:                              # 非法模式（空等）
            return ToolResult(text=f"glob 模式无效: {e}")
        return ToolResult(text="\n".join(hits) if hits else "无匹配")
