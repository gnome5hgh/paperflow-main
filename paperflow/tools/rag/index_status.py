"""IndexStatusTool：索引体检——对照状态文件、向量库、关键词索引与语料根。

索引器（rag-agent）的**只读**诊断工具。回答「库里现在到底有什么、和语料是否一致」：
已索引多少篇、多少块、有没有删除未收敛的残留（幽灵）、有没有新增未入库的文件、
状态文件与当前切块配方是否一致、PDF 里有多少篇走了 GROBID 降级解析。

不写任何东西、不触发重扫。查出不一致时由调用方决定是否跑一次 reindex_all 收敛。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service

#: 幽灵 / 未入库清单在输出里的展示上限（超出只报总数，避免几十篇刷屏）
_LIST_CAP = 10


class IndexStatusTool(Tool):
    """索引体检（只读，不触发写入）。

    Attributes:
        name: str，工具名 "index_status"
        description: str，工具描述
        parameters: dict，JSON Schema（无参数）
        risk_level: str，"low"（只读）
        requires_confirm: bool，False
        side_effects: list[str]，["read_file"]（读状态文件与语料目录元数据）
    """

    name = "index_status"
    description = (
        "体检语料索引：报告已索引文档数与块数、状态文件与当前切块配方是否一致、"
        "删除后未收敛的残留（幽灵块）、新增未入库的文件、以及 PDF 的解析器分布。"
        "只读、不触发重建。用户问「索引里有什么 / 索引好像不对 / 是不是漏了几篇」时先用它，"
        "再据结论决定是否调 reindex_all 收敛。")
    parameters = {"type": "object", "properties": {}, "required": []}
    risk_level = "low"
    requires_confirm = False
    side_effects = ["read_file"]

    def execute(self) -> ToolResult:
        """取一次索引体检快照并格式化成文本。

        Returns:
            ToolResult，文本为各项计数与不一致清单；服务不可用时返回错误文本
            （不中断调用方流程，由上级决定是否请示用户）。
        """
        try:
            st = get_rag_service().index_status()
        except Exception as e:
            # 与 reindex_all 同款：外部服务缺席如实回报，不让异常穿透 ReAct 循环
            return ToolResult(text=f"索引体检失败：{e}", is_error=True)

        lines = [
            f"向量库：{'可连' if st.milvus_ok else '不可连（库侧计数不可用，建议先起 Milvus）'}"
            + (f"，{st.store_chunks} 块 / {st.store_docs} 篇" if st.milvus_ok else ""),
            f"关键词索引（BM25）：{st.bm25_docs} 篇" if st.milvus_ok else "关键词索引：未读（库不可连）",
        ]
        if not st.state_present:
            lines.append(
                f"状态文件：不存在或不可解析（语料根下现见 {st.corpus_docs} 篇，"
                "从未建过索引或状态损坏）→ 跑一次 reindex_all 会全量重扫")
        else:
            lines.append(f"状态文件：记录 {st.indexed_docs} 篇；语料根下现见 {st.corpus_docs} 篇")
            lines.append(
                "切块配方：与当前配置一致" if st.recipe_in_sync
                else "切块配方：**与当前配置不一致**（切块参数或解析逻辑改过）"
                     "→ 下次 reindex_all 会全量重扫重嵌")
            if st.parsers:
                dist = "、".join(f"{k} {v} 篇" for k, v in sorted(st.parsers.items()))
                lines.append(f"PDF 解析器分布：{dist}（pymupdf 为 GROBID 降级）")

        if st.ghost:
            lines.append(f"⚠ 幽灵块 {len(st.ghost)} 篇（已删除但索引残留）：{_fmt(st.ghost)}"
                         "→ 跑 reindex_all 收敛")
        if st.not_indexed:
            lines.append(f"⚠ 未入库 {len(st.not_indexed)} 篇（在语料根但不在索引）：{_fmt(st.not_indexed)}"
                         "→ 跑 index_paths 补入或 reindex_all 收敛")
        if not st.ghost and not st.not_indexed and st.state_present:
            lines.append("一致性：索引与语料一致，无残留、无遗漏")

        return ToolResult(text="\n".join(lines), summary={
            "milvus_ok": st.milvus_ok, "recipe_in_sync": st.recipe_in_sync,
            "indexed_docs": st.indexed_docs, "corpus_docs": st.corpus_docs,
            "store_chunks": st.store_chunks, "store_docs": st.store_docs,
            "bm25_docs": st.bm25_docs, "parsers": st.parsers,
            "ghost": len(st.ghost), "not_indexed": len(st.not_indexed)})


def _fmt(items: list) -> str:
    """把路径清单压成一行；超出上限只报总数与头几条。"""
    shown = "、".join(items[:_LIST_CAP])
    return shown if len(items) <= _LIST_CAP else f"{shown} …等 {len(items)} 篇"
