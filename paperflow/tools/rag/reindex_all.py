"""ReindexAllTool：全量增量扫描——补缺、清陈旧、重建 BM25。

索引器（indexer agent）的冷路径工具，覆盖三类「定点入库管不到」的场景：
删除后的收敛（删除只能靠状态文件比对发现，已不存在的文件无法逐条入库）、
手动拷进语料根的文件回填、切块配方变更后的整体重建。幂等：重复调用只在
确有变更时产生写入。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service


class ReindexAllTool(Tool):
    """全量收敛索引的写入类工具（不写用户文件）。

    Attributes:
        name: str，工具名 "reindex_all"
        description: str，工具描述
        parameters: dict，JSON Schema（无参数）
        risk_level: str，"medium"（写索引库）
        requires_confirm: bool，False（幂等、不删除用户内容，用户已表达重建意图）
        side_effects: list[str]，["network"]（编码走云端嵌入）
    """

    name = "reindex_all"
    description = (
        "对整个语料库做一次增量收敛：重新索引新增或有改动的笔记 / PDF，"
        "清理已删除文件残留的索引块，并重建关键词索引。"
        "用于删除文件之后、手动往语料目录里拷了论文之后、或怀疑索引与语料不一致时。")
    parameters = {"type": "object", "properties": {}, "required": []}
    risk_level = "medium"
    requires_confirm = False
    side_effects = ["network"]

    def execute(self) -> ToolResult:
        """跑一次全量增量扫描并回报统计。

        Returns:
            ToolResult，文本为变更/清理/块数合计；失败时返回错误文本。
        """
        try:
            out = get_rag_service().index_all()
        except Exception as e:
            # 外部服务缺席不该以异常穿透到 ReAct 循环：如实回报，由上级决定是否请示用户
            return ToolResult(text=f"索引收敛失败：{e}", is_error=True)
        note = "（配方已变更，本次全量重扫）" if out.recipe_reset else ""
        return ToolResult(
            text=(f"索引收敛完成：重索引 {out.changed} 篇、清理已删除 {out.removed} 篇、"
                  f"写入 {out.chunks} 块、关键词索引 {out.bm25_docs} 篇{note}"),
            summary={"changed": out.changed, "removed": out.removed,
                     "chunks": out.chunks, "bm25_docs": out.bm25_docs,
                     "recipe_reset": out.recipe_reset})
