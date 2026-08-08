# paperflow/tools/grep.py
"""GrepTool：在文件/目录内按正则搜文本（只读）。

edit_file 的 search-replace 锚点确认、reviewer 事实核对、searcher 下载校验。
只读 → low、无确认。目录递归只搜文本文件（md/txt/py/jsonl），跳过二进制/pdf。
"""
import re
from pathlib import Path

from paperflow.core.security.workspace import is_denied_path
from paperflow.core.tool import Tool, ToolResult

_TEXT_SUFFIXES = (".md", ".txt", ".py", ".jsonl")


class GrepTool(Tool):
    name = "grep"
    description = ("在文件或目录内搜索文本（正则），返回 file:line 匹配行。"
                   "用于确认锚点文本、核对内容。目录递归搜索 md/txt 等文本文件。")
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式（如 'Disentangled Progressive'）"},
            "path": {"type": "string", "format": "path",
                     "description": "文件或目录（目录递归，限 note/pdf/memory 根）"},
        },
        "required": ["pattern", "path"],
    }
    risk_level = "low"
    allowed_roots = ["note", "pdf", "memory"]

    def execute(self, pattern: str, path: str) -> ToolResult:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(text=f"正则无效: {e}")
        p = Path(path)
        # 敏感路径黑名单：目录递归时跳过 workspace/audit 等 deny 目录（防通配符摸审计日志/密钥）。
        # cfg 防御式读取——测试与裸构造时可能没有 _config（与 glob 一致）。
        cfg = getattr(self, "_config", None)
        files = [p] if p.is_file() else [
            f for f in p.rglob("*")
            if f.suffix.lower() in _TEXT_SUFFIXES
            and (cfg is None or not is_denied_path(f.resolve(), cfg.workspace))
        ]
        results: list[str] = []
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue    # 非文本/权限问题跳过，不阻断整体搜索
            for i, line in enumerate(lines, 1):
                if regex.search(line):
                    # 返回原始行（不 strip、不截断）：grep 命中会被当作 edit_file 的
                    # old_text 锚点，锚点必须与文件逐字节一致（缩进/超长行也不例外），
                    # strip/截断会让模型复制过去的 old_text 匹配失败（Important 4）。
                    # 行可能很长，但作为锚点需要原样；用 30 条封顶控制量。
                    results.append(f"{f}:{i}: {line}")
                    if len(results) >= 30:                       # 封顶 30 条
                        return ToolResult(text="\n".join(results))
        return ToolResult(text="\n".join(results) if results else "无匹配")
