"""sync_citations：语料库论文全量同步进 references.bib（幂等增量）。"""
from paperflow.core.tool import Tool, ToolResult


class SyncCitationsTool(Tool):
    name = "sync_citations"
    description = ("把语料库论文全量同步进 references.bib。幂等：已在库的按标题去重跳过；"
                   "缺作者/年份的拒绝入库并在报告中列出（可启动 GROBID 后重跑补齐）。")
    parameters = {"type": "object", "properties": {}}
    risk_level = "medium"               # 批量追加但不删数据、可重入，不升 high
    side_effects = ["write_file"]

    def __init__(self, manager):
        self.manager = manager

    def execute(self) -> ToolResult:
        r = self.manager.sync_all()
        text = (f"扫描 {r['total']} 篇：新增 {len(r['added'])}、"
                f"已在库跳过 {len(r['skipped'])}、拒绝 {len(r['rejected'])}")
        if r["rejected"]:
            lines = "\n".join(f"- {item['pdf_path']}: {item['note']}"
                              for item in r["rejected"])
            text += f"\n拒绝入库明细（缺元数据，可启动 GROBID 后重跑）：\n{lines}"
        summary = {"total": r["total"], "added": len(r["added"]),
                   "skipped": len(r["skipped"]), "rejected": len(r["rejected"])}
        return ToolResult(text=text, summary=summary)
