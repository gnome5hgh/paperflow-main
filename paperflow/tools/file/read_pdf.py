"""ReadPdfTool：GROBID 解析 PDF 为结构化文本，不可用时回退 PyMuPDF。

经 RAGService.pdf_parser() 获取解析器（GROBID 探测结果缓存于服务层，当次会话不回退）。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service


def _normalize_path(p: str) -> str:
    """归一化：连续空白折叠为单空格 + 小写。D4 模糊匹配用——LLM 空格折叠
    （双空格→单空格）后的请求路径与实际文件比，归一化后一致。"""
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
        try:
            # 精确路径优先（缓存入口，reviewer 每轮复用——见 2026-08-04
            # writer-timeout-fix）。exact 成功零行为变化（D4 承诺）。
            doc = get_rag_service().parse_pdf_cached(path)
        except (FileNotFoundError, OSError):
            # 精确 miss → 容错分支（D4，RC2b）：LLM 可能折叠路径空白（双空格文件名
            # 被归一成单空格），按归一化 basename 在 pdf root 下找唯一命中。
            try:
                doc = self._resolve_fuzzy(path)
            except (FileNotFoundError, ValueError) as e:
                # G（2026-08-06）：对齐 read_file——0/多候选 raise，_exec_tool 捕获转
                # ToolResult("Tool error: ...") + success=False。旧行为返回含错误文本的
                # ToolResult，审计 success=True 误导（agent 分不清读成功还是读到错误，
                # 放大 writer 的路径猜测风暴 P2）。LLM 仍看到可读错误文本
                #（经 _exec_tool 的 "Tool error:" 包装）。
                raise e
        text = "\n\n".join(f"## {h}\n{t}" for h, t in doc.sections)
        return ToolResult(text=text or "（PDF 未能解析出文本）")

    def _resolve_fuzzy(self, path: str):
        """精确路径失败时的容错解析。安全语义：唯一命中才用、不猜。

        0 候选 → 明确"未找到"；多候选 → 明确"不唯一"交 LLM 澄清。只对 pdf root
        （config.vault_pdf_dir）递归搜索，不外扩。命中后仍走 parse_pdf_cached（缓存）。"""
        cfg = get_rag_service().config
        root = Path(cfg.vault_pdf_dir)
        # 归一化目标取 basename 而非全路径：LLM 空格折叠只影响文件名本身，子目录层级
        # 不应参与匹配——否则同 basename 异目录的文件会被全路径比较误判为唯一命中，
        # 该不唯一的场景本应报"不唯一"交 LLM 澄清（D4 安全语义，不猜）。
        target = _normalize_path(Path(path).name)
        hits = [f for f in root.rglob("*.pdf") if _normalize_path(f.name) == target]
        if len(hits) == 1:
            return get_rag_service().parse_pdf_cached(str(hits[0]))
        if not hits:
            raise FileNotFoundError(f"PDF 未找到: {path}")
        raise ValueError(f"PDF 路径不唯一（{len(hits)} 个候选），请明确指定: {path}")
