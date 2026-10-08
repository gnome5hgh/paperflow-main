"""sync_citations：语料库论文全量同步进 references.bib（幂等增量）。"""
from paperflow.core.tool import Tool, ToolResult


class SyncCitationsTool(Tool):
    """语料库论文全量同步进 references.bib 的工具（幂等增量）。

    Attributes:
        name: str，工具名 "sync_citations"
        description: str，工具描述
        parameters: dict，空参数 schema
        risk_level: str，"medium"（批量追加不删数据、可重入）
        side_effects: list[str]，["write_file"]
        manager: CitationManager，注入的引用库门面
    """
    name = "sync_citations"
    description = ("把语料库论文全量同步进 references.bib。幂等：已在库的按标题去重跳过；"
                   "缺作者/年份的拒绝入库并在报告中列出（可启动 GROBID 后重跑补齐）。")
    parameters = {"type": "object", "properties": {}}
    risk_level = "medium"               # 批量追加但不删数据、可重入，不升 high
    side_effects = ["write_file"]

    def __init__(self, manager):
        """注入引用库门面。

        Args:
            manager: CitationManager，引用库读写入口
        """
        self.manager = manager

    def effective_target_path(self, args: dict) -> str | None:
        """导出写互斥键：引用库文件。

        三个引用工具写的是同一个 references.bib；本工具的参数是条目内容 / 键名，没有
        path。经它键控后，两路并发写同一个 bib 会当场被拒，而不是被底层写入锁静默串行
        ——并发因此是可见的，而不是悄悄发生。

        Args:
            args: dict，已解析的工具调用参数（未使用，仅为签名与基类一致）

        Returns:
            references.bib 的绝对路径。
        """
        return str(self.manager.bib_path)

    def execute(self) -> ToolResult:
        """全量同步并把扫描/新增/跳过/拒绝统计回传。

        Returns:
            ToolResult，文本为统计摘要与拒绝入库明细；summary 带四个计数。
        """
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
