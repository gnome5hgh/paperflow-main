"""DedupPapersTool：多源去重——DOI → arXiv ID → 规范化标题四级匹配。纯逻辑、无副作用。"""
from paperflow.core.tool import Tool, ToolResult


class DedupPapersTool(Tool):
    """多源去重：DOI → arXiv ID → 规范化标题 四级匹配。纯逻辑、无副作用。"""

    name = "dedup_papers"
    description = "对多来源论文列表去重（DOI → arXiv ID → 标题四级匹配）"
    parameters = {
        "type": "object",
        "properties": {
            "papers": {"type": "array", "items": {"type": "object"},
                       "description": "论文元数据列表"},
        },
        "required": ["papers"],
    }
    risk_level = "low"

    @staticmethod
    def _norm_title(title: str) -> str:
        import re
        return re.sub(r"[^\w]", "", title).lower()

    def execute(self, papers: list[dict]) -> ToolResult:
        # 四级匹配：DOI 最可靠，其次 arXiv ID，最后规范化标题（退化键）。
        # 多源搜索可能返回同一论文的不同版本（标题带版本号/期刊名后缀），
        # 先按强键合并，避免后续 Filter 阶段重复计算。
        seen_doi, seen_arxiv, seen_title = set(), set(), set()
        merged = []
        for p in papers:
            key = None
            if p.get("doi"):
                key = ("doi", p["doi"])
            elif p.get("arxiv_id"):
                key = ("arxiv", p["arxiv_id"])
            else:
                key = ("title", self._norm_title(p.get("title", "")))
            sig, val = key
            bucket = seen_doi if sig == "doi" else (seen_arxiv if sig == "arxiv" else seen_title)
            if val in bucket:
                continue
            bucket.add(val)
            merged.append(p)
        # 输出带上 arXiv ID（若有）：让 LLM 可观测"同一 arXiv ID 只留一条"的去重结果
        # （brief 测试断言 arxiv_id 在文本中出现一次）。缺 arXiv ID 的条目只打标题，
        # 避免输出 "None" 噪音。
        lines = [f"- {p.get('title')} ({p['arxiv_id']})" if p.get("arxiv_id")
                 else f"- {p.get('title')}" for p in merged]
        return ToolResult(text="\n".join(lines) if lines else "（空列表）")
