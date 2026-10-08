"""ReadPdfTool：本地解析 PDF 为 markdown 文本，不依赖任何外部服务。

解析走 PyMuPDF 直读（见 ``pdf_extract``）。以前这里绕道检索服务栈取解析结果，代价是
读一篇论文要排在索引/检索共用的全局锁后面、还要看 GROBID 服务与网络是否配合——读本地
文件本不该有这些牵扯。带 TEI 结构化语义的解析仍归 RAG 模块，服务向量库索引与语料标题
索引，与本工具互不影响。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.file.pdf_extract import extract_pdf


def _normalize_path(p: str) -> str:
    """归一化路径:连续空白折叠为单空格 + 小写。用于模糊匹配——LLM 可能把双空格
    文件名折叠成单空格,归一化后与真实文件名一致。

    Args:
        p: str，路径或文件名

    Returns:
        折叠连续空白并小写后的字符串（模糊匹配用）。
    """
    return " ".join(p.split()).lower()


class ReadPdfTool(Tool):
    """解析 PDF 为 markdown 文本的只读工具（本地 PyMuPDF 直读）。

    Attributes:
        name: str，工具名 "read_pdf"
        description: str，工具描述
        parameters: dict，JSON Schema（path）
        risk_level: str，"low"（只读）
        root_hints: list[str]，["pdf"]
        output_scan: str，"mark"
        side_effects: list[str]，["read_file"]
    """

    name = "read_pdf"
    description = "解析 PDF 论文为文本（本地提取，章节标题按版面还原）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "PDF 绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    root_hints = ["pdf"]                    # 提示语料库 PDF 根（只读语义在中间件）
    output_scan = "mark"
    side_effects = ["read_file"]

    def execute(self, path: str) -> ToolResult:
        """解析 PDF 为 markdown 文本,顶部带论文标题;解析不出内容时给出提示。

        精确路径优先(走抽取缓存,同一篇被反复读时只解析一次);精确失败才走容错分支
        ——LLM 可能折叠路径空白,按归一化 basename 在 pdf 根下找唯一命中。

        Args:
            path: str，PDF 文件绝对路径

        Returns:
            ToolResult，文本为「标题 + 正文 markdown」；解析不出内容时返回提示文本。
        """
        try:
            doc = extract_pdf(path)
        except (FileNotFoundError, OSError):
            # 精确 miss → 容错分支:LLM 可能折叠路径空白(双空格文件名被归一成单空格),
            # 按归一化 basename 在 pdf 根下找唯一命中。
            try:
                doc = self._resolve_fuzzy(path)
            except (FileNotFoundError, ValueError) as e:
                # 0/多候选直接上抛,由执行器转为错误结果——旧行为返回含错误文本的结果
                # 会被审计记为成功,agent 分不清读成功还是读到错误。LLM 仍能看到可读
                # 错误文本(经执行器的 "Tool error:" 包装)。
                raise e
        text = doc.body
        if doc.title:
            text = f"# {doc.title}\n\n" + text   # 标题在顶部，noter 据此拿干净全标题
        return ToolResult(text=text or "（PDF 未能解析出文本）")

    def _resolve_fuzzy(self, path: str):
        """精确路径失败时的容错解析。安全语义:唯一命中才用、不猜。

        0 候选 → 明确"未找到";多候选 → 明确"不唯一"交 LLM 澄清。搜索根取语料库 pdf 根
        （由装配注入的配置给出）；未注入配置时退回请求路径所在目录——直接把根当成当前
        工作目录去递归会让结果不可预期。

        Args:
            path: str，精确解析失败的原始路径

        Returns:
            唯一命中时返回解析结果；0 候选抛 FileNotFoundError，多候选抛 ValueError（交 LLM 澄清）。
        """
        cfg = getattr(self, "_config", None)
        pdf_dir = getattr(getattr(cfg, "corpus", None), "pdf_dir", "") if cfg is not None else ""
        root = Path(pdf_dir) if pdf_dir else Path(path).parent
        # 归一化目标取 basename 而非全路径:LLM 空格折叠只影响文件名本身,子目录层级
        # 不应参与匹配——否则同 basename 异目录的文件会被全路径比较误判为唯一命中,
        # 该不唯一的场景本应报"不唯一"交 LLM 澄清(安全语义,不猜)。
        target = _normalize_path(Path(path).name)
        hits = [f for f in root.rglob("*.pdf") if _normalize_path(f.name) == target]
        if len(hits) == 1:
            return extract_pdf(str(hits[0]))
        if not hits:
            raise FileNotFoundError(f"PDF 未找到: {path}")
        raise ValueError(f"PDF 路径不唯一（{len(hits)} 个候选），请明确指定: {path}")
