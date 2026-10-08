# paperflow/tools/file/grep.py
"""GrepTool：在文件/目录内按正则搜文本（只读）。

edit_file 的 search-replace 锚点确认、reviewer 事实核对、searcher 下载校验。
只读 → low、无确认。目录递归只搜文本文件（md/txt/py/jsonl），跳过二进制/pdf。
"""
import re
from pathlib import Path

from paperflow.core.security.middleware.workspace import is_denied_path
from paperflow.core.tool import Tool, ToolResult

_TEXT_SUFFIXES = (".md", ".txt", ".py", ".jsonl")


class GrepTool(Tool):
    """在文件/目录内按正则搜文本的只读工具（目录递归只搜文本文件）。

    Attributes:
        name: str，工具名 "grep"
        description: str，工具描述
        parameters: dict，JSON Schema（pattern/path）
        risk_level: str，"low"（只读）
        root_hints: list[str]，["note", "pdf", "memory"]
    """
    name = "grep"
    description = ("在文件或目录内搜索文本（正则），返回 file:line 匹配行。"
                   "用于确认锚点文本、核对内容。目录递归搜索 md/txt 等文本文件。")
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式（如 'Disentangled Progressive'）"},
            "path": {"type": "string", "format": "path",
                     "description": "文件或目录绝对路径（目录递归；缺省=语料库笔记根）"},
        },
        "required": ["pattern"],
    }
    risk_level = "low"
    root_hints = ["note", "pdf", "memory"]

    def execute(self, pattern: str, path: str | None = None) -> ToolResult:
        """在文件或目录内按正则搜索文本,返回 file:line 匹配行(最多 30 条)。

        Args:
            pattern: 正则表达式
            path: 文件路径或目录(目录递归,只搜文本文件,跳过二进制/PDF);缺省=语料库笔记根

        Returns:
            匹配行每行一条(file:line: 原文);无匹配返回"无匹配"

        """
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(text=f"正则无效: {e}")
        # cfg 防御式读取(测试与裸构造时可能没有 _config)。敏感路径黑名单:
        # 目录递归时跳过审计/密钥等目录,防通配符枚举。
        cfg = getattr(self, "_config", None)
        if path:
            p = Path(path)
        else:
            default_root = cfg.corpus.note_dir if cfg is not None else ""
            if not default_root:
                return ToolResult(text="未传 path 且语料库笔记根未配置——请显式传 path 绝对路径")
            p = Path(default_root)
        files = [p] if p.is_file() else [
            f for f in p.rglob("*")
            if f.suffix.lower() in _TEXT_SUFFIXES
            and (cfg is None or not is_denied_path(f.resolve(), cfg.runtime.workspace))
        ]
        results: list[str] = []
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue    # 非文本/权限问题跳过,不阻断整体搜索
            for i, line in enumerate(lines, 1):
                if regex.search(line):
                    # 返回原始行(不 strip/截断):grep 命中会当作 edit_file 的 old_text
                    # 锚点,锚点必须与文件逐字节一致,strip/截断会让模型复制过去的锚点
                    # 匹配失败。行可能很长,但作为锚点需要原样;用 30 条封顶控制量。
                    results.append(f"{f}:{i}: {line}")
                    if len(results) >= 30:                       # 封顶 30 条
                        return ToolResult(text="\n".join(results))
        return ToolResult(text="\n".join(results) if results else "无匹配")
