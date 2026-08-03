"""FilterPapersTool：综合筛选——年份/引用数/期刊白名单。阈值由 LLM 传参；期刊白名单可省略。"""
from paperflow.core.tool import Tool, ToolResult


class FilterPapersTool(Tool):
    """综合筛选：年份/引用数/期刊白名单。阈值由 LLM 传参；期刊白名单可省略。"""

    name = "filter_papers"
    description = "按年份/引用数/期刊筛选论文列表"
    parameters = {
        "type": "object",
        "properties": {
            "papers": {"type": "array", "items": {"type": "object"}},
            "year_min": {"type": "integer", "description": "最小年份（可选）"},
            "min_citations": {"type": "integer", "description": "最小引用数（可选）"},
            "journals": {"type": "array", "items": {"type": "string"},
                         "description": "期刊白名单（可选）"},
        },
        "required": ["papers"],
    }
    risk_level = "low"

    def execute(self, papers: list[dict], year_min: int | None = None,
                min_citations: int | None = None,
                journals: list[str] | None = None) -> ToolResult:
        out = []
        for p in papers:
            if year_min is not None and (p.get("year") or 0) < year_min:
                continue
            if min_citations is not None and (p.get("cited_by_count") or 0) < min_citations:
                continue
            # journals 白名单可省略：p 缺 journal 字段时 (p.get("journal") is None)
            # 不 ∈ journals → 会被筛掉。但 OpenAlex 条目无 journal 信息是常态，
            # 若 LLM 未传白名单就不应自动淘汰，故用 `journals and ...` 短路。
            if journals and p.get("journal") not in journals:
                continue
            out.append(p)
        lines = [f"- {p.get('title')}" for p in out]
        return ToolResult(text="\n".join(lines) if lines else "（无满足条件的论文）")
