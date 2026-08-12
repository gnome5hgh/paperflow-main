"""ReadPdfTool：GROBID 解析 PDF 为结构化文本，不可用时回退 PyMuPDF。

经 RAGService.pdf_parser() 获取解析器（GROBID 探测结果缓存于服务层，当次会话不回退）。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service


def _normalize_path(p: str) -> str:
    """归一化路径:连续空白折叠为单空格 + 小写。用于模糊匹配——LLM 可能把双空格
    文件名折叠成单空格,归一化后与真实文件名一致。"""
    return " ".join(p.split()).lower()


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
        """解析 PDF 为结构化文本,按章节拼接返回;解析不出内容时给出提示。

        精确路径优先(走解析缓存,reviewer 每轮审稿复用同一份解析);精确失败才走
        容错分支——LLM 可能折叠路径空白,按归一化 basename 在 pdf 根下找唯一命中。
        """
        try:
            doc = get_rag_service().parse_pdf_cached(path)
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
        text = "\n\n".join(f"## {h}\n{t}" for h, t in doc.sections)
        return ToolResult(text=text or "（PDF 未能解析出文本）")

    def _resolve_fuzzy(self, path: str):
        """精确路径失败时的容错解析。安全语义:唯一命中才用、不猜。

        0 候选 → 明确"未找到";多候选 → 明确"不唯一"交 LLM 澄清。只对 pdf 根递归
        搜索,不外扩。命中后仍走解析缓存。"""
        cfg = get_rag_service().config
        root = Path(cfg.vault_pdf_dir)
        # 归一化目标取 basename 而非全路径:LLM 空格折叠只影响文件名本身,子目录层级
        # 不应参与匹配——否则同 basename 异目录的文件会被全路径比较误判为唯一命中,
        # 该不唯一的场景本应报"不唯一"交 LLM 澄清(安全语义,不猜)。
        target = _normalize_path(Path(path).name)
        hits = [f for f in root.rglob("*.pdf") if _normalize_path(f.name) == target]
        if len(hits) == 1:
            return get_rag_service().parse_pdf_cached(str(hits[0]))
        if not hits:
            raise FileNotFoundError(f"PDF 未找到: {path}")
        raise ValueError(f"PDF 路径不唯一（{len(hits)} 个候选），请明确指定: {path}")
