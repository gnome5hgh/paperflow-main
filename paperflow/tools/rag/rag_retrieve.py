"""RagRetrieveTool：暴露给外部调用方的 RAG 检索工具（薄封装）。

只做三件事：惰性取全局 RAGService 单例、持锁调用检索器、把检索结果格式化成
人类可读文本。query 先经 QueryRewriter 改写（锁外，一次 LLM 调用，失败降级原
query），再持锁检索。检索与融合算法本身在 `rag/services/retriever.py` 的 Retriever。
"""
import logging

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.constants import DEFAULT_TOP_K
from paperflow.rag.services.rag_service import get_rag_service
from paperflow.tools.memory.runtime_context import get_memory_context

logger = logging.getLogger(__name__)


def _recent_history(limit: int) -> list:
    """取最近对话历史（condense 改写输入）：memory 系统的 in-context 窗口投影。

    经 memory 工具运行时上下文取 message_manager + agent_id，读该 agent 的
    in-context 消息（与模型所见一致，含压缩摘要），只留 user/assistant 且
    内容非空的尾部 limit 条（limit 由调用方从 rag.query_rewrite.history_messages
    传入，改 YAML 即生效）。上下文未绑定/manager 缺失/读取异常一律返回 []——
    历史读取永远不打断检索（spec §6 降级铁律）。
    """
    ctx = get_memory_context()
    if ctx is None or getattr(ctx, "message_manager", None) is None:
        return []
    try:
        msgs = ctx.message_manager.get_in_context_messages(ctx.agent_id)
        return [m for m in msgs
                if getattr(m, "role", None) in ("user", "assistant")
                and (getattr(m, "content", None) or "").strip()
                ][-limit:]
    except Exception:
        logger.warning("读取对话历史失败，本次检索跳过 condense 改写", exc_info=True)
        return []


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
                   "参数 query 为检索问题；top_k 为返回块数；source 可选限定来源——"
                   "问笔记观点用 \"note\"，问论文原文用 \"pdf\"，缺省两处都搜。")
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题"},
            "top_k": {"type": "integer", "description": "返回块数",
                      "default": DEFAULT_TOP_K},
            "source": {"type": "string", "enum": ["note", "pdf"],
                       "description": "限定来源：note=读书笔记，pdf=论文原文；缺省不限"},
        },
        "required": ["query"],
    }
    risk_level = "low" # 工具风险等级：只读操作，无风险

    def __init__(self):
        """创建检索工具；_service 延迟到首次 execute 时取全局单例（也支持测试注入）。"""
        super().__init__()
        self._service = None # 可被测试注入，否则在 execute 中取全局单例

    def execute(self, query: str, top_k: int | None = None,
                source: str | None = None) -> ToolResult:
        """执行检索并返回格式化结果：每条命中列出来源、路径与正文摘录（前 N 字）。

        带标题前缀的块其摘录首行即「论文标题 > 章节标题」，供上层直接引用节号。
        摘录上限、默认 top_k、历史条数全部读配置（rag.tools.excerpt_chars /
        rag.retriever.top_k / rag.query_rewrite.history_messages），改 YAML 即生效。

        Args:
            query: 检索查询。
            top_k: 返回块数；None 时取 rag.retriever.top_k（schema default 是给
                   模型看的提示，运行期以配置为准）。
            source: 限定来源——"note" 只搜笔记，"pdf" 只搜论文；None 不过滤。

        Returns:
            ToolResult: 包含格式化文本的 ToolResult 对象。
        """
        # 1. 获取 RAGService 单例（若已注入则使用注入的实例）。
        svc = self._service or get_rag_service()
        if top_k is None:
            top_k = svc.config.rag.retriever.top_k

        # 1.5 锁外改写（spec 2026-10-04 §5.3）：LLM 调用慢且不碰共享检索状态，
        # 不能占着 svc.lock 阻塞索引/其他检索；任何失败降级为 [原query]。
        queries = [query]
        rewriter = getattr(svc, "get_rewriter", None)
        if rewriter is not None:
            try:
                history = _recent_history(
                    svc.config.rag.query_rewrite.history_messages)
                queries = rewriter().rewrite(query, history).queries
            except Exception as e:
                logger.warning("query 改写失败，降级为原始 query 检索：%s", e)

        # 2. 持锁调用检索器（保证与索引操作的互斥）。Milvus 中途崩溃（真实使用
        # 测试 P1-4：容器 Exited(1) 静默降级 3.5 小时无人知晓）时异常透传会变成
        # 千篇一律的 Tool error——这里捕获并返回固定降级声明，让上层明确知道
        # 「检索结果可能不完整/不可用」而非怀疑工具本身。
        try:
            with svc.lock:
                # source 原样透传给检索器（非法值由 Retriever 侧按不过滤防御处理）。
                chunks = svc.get_retriever().retrieve(queries, top_k, source)
        except Exception as e:
            return ToolResult(
                text="⚠️ 向量检索不可用（Milvus 异常），本次检索失败，结果可能不完整。"
                     f"原始错误：{e}。可尝试 `docker compose up -d` 重启依赖服务后重试；"
                     "或基于已有资料继续，如实说明检索不可用。",
                is_error=True)

        # 3. 若无结果，返回结构化提示信息。
        if not chunks:
            return ToolResult(text="检索无命中（索引可能为空，可先写几篇笔记）")

        # 4. 否则，每条命中格式化为 `- [来源:路径] 正文摘录前 N 字` 的列表。
        # 摘录上限读 rag.tools.excerpt_chars：带前缀的块首行即「论文标题 > 章节标题」，
        # 需要足够窗口才能让上层同时拿到节号与可用的正文上下文。
        excerpt_chars = svc.config.rag.tools.excerpt_chars
        lines = [f"- [{c.source}:{c.path}] {c.text[:excerpt_chars]}" for c in chunks]
        return ToolResult(text="检索到以下相关段落：\n" + "\n".join(lines))
