"""ReadFileTool：读取资料库内各类根目录下的文本文件。

安全边界由中间件强制:path 须为绝对路径且不在敏感路径黑名单(workspace/audit、
.git 等),读取外部内容会打"未经安全校验"横幅。工具自身不重复校验——声明元数据即可。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult


class ReadFileTool(Tool):
    """读取语料库内文本文件的只读工具。

    Attributes:
        name: str，工具名 "read_file"
        description: str，工具描述
        parameters: dict，JSON Schema（path）
        risk_level: str，"low"（只读）
        root_hints: list[str]，["note", "pdf", "memory", "scratch", "research"]
        provenance: str，"local_file"（外部文件内容打未校验横幅）
        side_effects: list[str]，["read_file"]
    """
    name = "read_file"
    description = ("读取文本文件内容（UTF-8）。只能读文本：论文 PDF 用 read_pdf，"
                   "目录用 glob 列出文件后再逐个读。")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "文件绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    # scratch（子 agent 读落盘桥草稿）
    root_hints = ["note", "pdf", "memory", "scratch", "research"]
    provenance = "local_file"                  # 外部文件内容 → 内容防护打来源标记
    side_effects = ["read_file"]

    def execute(self, path: str) -> ToolResult:
        """读取 path 指向的文本文件并原样返回(编码固定为 UTF-8)。

        读到非文本内容（PDF/二进制）或目录时，返回**纠正性错误文本**而不是裸异常：
        异常文本只说明失败了什么，引导文本直接告诉模型下一步该调哪个工具，
        省掉一次「报错 → 猜 → 重试」。

        Args:
            path: str，文件绝对路径

        Returns:
            ToolResult，文本为文件内容；目录/PDF/二进制/不可读时返回引导性错误文本。
        """
        target = Path(path)
        if target.is_dir():
            return ToolResult(
                text=(f"{path} 是目录，不是文件。请用 glob 列出目录下的文件"
                      f"（glob(pattern=\"{path}/*\")），再对具体文件调用 read_file。"),
                is_error=True)
        if target.suffix.lower() == ".pdf":
            return ToolResult(
                text=f"{path} 是 PDF 文件，read_file 只读 UTF-8 文本。读取论文内容请用 read_pdf。",
                is_error=True)
        try:
            return ToolResult(text=target.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            return ToolResult(
                text=(f"{path} 不是 UTF-8 文本（可能是 PDF、图片或其他二进制文件），"
                      "read_file 读不了。论文用 read_pdf，图表用 analyze_figures/view_image。"),
                is_error=True)
        except OSError as e:
            return ToolResult(text=f"读取失败：{type(e).__name__}: {e}", is_error=True)
