"""IndexPathsTool：把指定语料文件入库或重索引。

索引器（indexer agent）的热路径工具：内容生产者写完或改完文件后派发它，
一次带上全部待入库路径。真正的切块、编码与写入由 ``RAGService.index_document``
在锁内完成，本工具只负责批量编排与把每一条的结果如实报出来。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service

#: 单次入库的路径条数上限。上限的作用是让超量请求显式失败并提示分批，
#: 而不是静默处理一批耗时很长的文档把子 agent 预算耗光。
MAX_PATHS = 50


class IndexPathsTool(Tool):
    """逐条入库给定路径的写入类工具（不写文件，只写索引）。

    Attributes:
        name: str，工具名 "index_paths"
        description: str，工具描述
        parameters: dict，JSON Schema（paths 数组）
        risk_level: str，"medium"（写索引库，不满足即超默认阈值被拒）
        requires_confirm: bool，False（幂等且不毁数据，不值得打断用户）
        side_effects: list[str]，["network"]（编码走云端嵌入）
    """

    name = "index_paths"
    description = (
        "把给定的语料文件（笔记 / PDF）切块、编码后写入检索索引。"
        "写盘或修改文件后调用它，一次传入全部待入库文件的绝对路径。"
        "路径不在语料根目录下、或文件不存在时会跳过并在结果里说明原因。")
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "待入库文件的绝对路径列表",
            },
        },
        "required": ["paths"],
    }
    risk_level = "medium"
    requires_confirm = False
    side_effects = ["network"]

    def execute(self, paths: list[str]) -> ToolResult:
        """逐条入库，单条失败不中断其余条目。

        Args:
            paths: list[str]，待入库文件的绝对路径

        Returns:
            ToolResult，文本为逐条结果与合计行；summary 带结构化计数
            （indexed / skipped / failed）。
        """
        if not paths:
            return ToolResult(text="未提供待入库路径", is_error=True)
        if len(paths) > MAX_PATHS:
            return ToolResult(
                text=f"一次最多入库 {MAX_PATHS} 个文件，本次 {len(paths)} 个，"
                     "请拆成多批",
                is_error=True)

        svc = get_rag_service()
        lines: list[str] = []
        indexed = skipped = 0
        failed: list[str] = []
        for p in paths:
            try:
                out = svc.index_document(p)
            except Exception as e:
                # 单条失败不拖垮整批：其余条目继续，失败项逐条列出
                failed.append(p)
                lines.append(f"- {p}：入库失败（{e}）")
                continue
            if out.status == "indexed":
                indexed += 1
                lines.append(f"- {p}：已入库 {out.chunks} 块")
            else:
                skipped += 1
                lines.append(f"- {p}：未入库（{out.reason}）")

        head = f"入库完成：成功 {indexed}、未入库 {skipped}、失败 {len(failed)}。"
        return ToolResult(text="\n".join([head, *lines]),
                          summary={"indexed": indexed, "skipped": skipped,
                                   "failed": failed})
