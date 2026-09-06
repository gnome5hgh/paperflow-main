"""RagRetrieveTool：暴露给外部调用方的 RAG 检索工具（薄封装）。

只做三件事：惰性取全局 RAGService 单例、持锁调用检索器、把检索结果格式化成
人类可读文本。检索与融合算法本身在 `rag/services/retriever.py` 的 Retriever。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service


class RagRetrieveTool(Tool):
    """对外暴露的检索工具：本身不实现检索逻辑，只惰性获取全局检索服务单例、持锁调用并格式化结果。

    职责：
    - 惰性获取全局 RAGService 单例。
    - 持有锁调用检索器。
    - 将检索结果格式化为人类可读的文本，供 Agent 或其他调用方使用。

    与 Retriever 的区别：
    - Retriever 实现核心检索算法。
    - RagRetrieveTool 是工具层封装，负责单例管理、锁控制和输出格式化。
    """

    name = "rag_retrieve"
    description = ("从本地知识库（笔记 + PDF 全文）检索相关段落。"
                   "参数 query 为检索问题，top_k 为返回块数。")
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题"},
            "top_k": {"type": "integer", "description": "返回块数", "default": 5},
        },
        "required": ["query"],
    }
    risk_level = "low" # 工具风险等级：只读操作，无风险

    def __init__(self):
        """创建检索工具；_service 延迟到首次 execute 时取全局单例（也支持测试注入）。"""
        super().__init__()
        self._service = None # 可被测试注入，否则在 execute 中取全局单例

    def execute(self, query: str, top_k: int = 5) -> ToolResult:
        """执行检索并返回格式化结果：每条命中列出来源、路径与文本前 200 字；无命中时给出提示。

        Args:
            query: 检索查询。
            top_k: 返回块数。

        Returns:
            ToolResult: 包含格式化文本的 ToolResult 对象。
        """
        # 1. 获取 RAGService 单例（若已注入则使用注入的实例）。
        svc = self._service or get_rag_service()

        # 2. 持锁调用检索器（保证与索引操作的互斥）。Milvus 中途崩溃（真实使用
        # 测试 P1-4：容器 Exited(1) 静默降级 3.5 小时无人知晓）时异常透传会变成
        # 千篇一律的 Tool error——这里捕获并返回固定降级声明，让上层明确知道
        # 「检索结果可能不完整/不可用」而非怀疑工具本身。
        try:
            with svc.lock:
                chunks = svc.get_retriever().retrieve(query, top_k)
        except Exception as e:
            return ToolResult(
                text="⚠️ 向量检索不可用（Milvus 异常），本次检索失败，结果可能不完整。"
                     f"原始错误：{e}。可尝试 `docker compose up -d` 重启依赖服务后重试；"
                     "或基于已有资料继续，如实说明检索不可用。",
                is_error=True)

        # 3. 若无结果，返回结构化提示信息。
        if not chunks:
            return ToolResult(text="检索无命中（索引可能为空，可先写几篇笔记）")

        # 4. 否则，每条命中格式化为 `- [来源:路径] 文本前200字` 的列表。
        lines = [f"- [{c.source}:{c.path}] {c.text[:200]}" for c in chunks]
        return ToolResult(text="检索到以下相关段落：\n" + "\n".join(lines))
