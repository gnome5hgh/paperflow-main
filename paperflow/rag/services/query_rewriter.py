# paperflow/rag/services/query_rewriter.py
"""QueryRewriter：检索前对 query 做一次 LLM 改写（condense + multi-query）。

condense（结合对话历史消指代、自包含化）与 multi-query 扩写合并为**恰好一次**
结构化 LLM 调用——检索路径同步持锁，少一次调用就少一份延迟。任何失败降级为
[原query] 单条检索（行为等同改写前），绝不打断检索。
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import BaseModel, Field

#: 改写结构化输出的解析失败重试次数（次）：0 = 不重试，失败即降级为 [原 query]，
#: 单次检索的失败延迟上限 = 1 次 LLM 调用，与业界主流做法一致。
REWRITE_MAX_RETRIES = 0

#: 单条历史消息截断长度（字符）：拼进改写 prompt 时每条消息保留的字符数。
HISTORY_MESSAGE_CHARS = 300

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是学术问答系统的检索查询改写器。根据对话历史（若有）与当前问题，"
    "输出用于知识库检索的查询集合。"
)

def _build_requirements(rewrite_num: int) -> str:
    """按 rewrite_num 生成改写要求段（条数写进 prompt，模型据此控制输出规模）。

    Args:
        rewrite_num: 要求模型给出的改写变体条数。

    Returns:
        拼进 prompt 的【要求】段文本。
    """
    return f"""
【要求】
1. standalone_query：把当前问题改写为自包含的检索查询——消解指代、补全省略，不依赖上文也能看懂；保持与当前问题相同的主要语言。
2. rewrites：给出 {rewrite_num} 条与 standalone_query 语义等价但措辞不同的检索查询，覆盖同义替换、关键词化、中英术语互补等角度；每条独立可检索，不要加解释。
3. 查询贴合学术语料（论文正文、读书笔记）的措辞，不要口语化表达。
"""


def _make_output_schema(rewrite_num: int) -> type[BaseModel]:
    """按 rewrite_num 生成结构化输出 schema（字段描述写明条数，与 prompt 对齐）。

    Args:
        rewrite_num: rewrites 字段的期望条数，写进字段描述。

    Returns:
        结构化输出模型类。
    """

    class RewriteOutput(BaseModel):
        """改写 LLM 的结构化输出 schema。

        Attributes:
            standalone_query: str，消解指代后的自包含检索查询
            rewrites: list[str]，与 standalone_query 语义等价但措辞不同的检索查询
        """

        standalone_query: str = Field(description="消解指代后的自包含检索查询，保持原问题主语言")
        rewrites: list[str] = Field(
            description=f"{rewrite_num} 条语义等价但措辞不同的检索查询")

    return RewriteOutput


@dataclass
class RewriteResult:
    """改写结果：queries 是最终查询集（queries[0] 即主查询，供 reranker 打分）。

    degraded=True 表示 LLM 失败已降级——queries 退化为 [原query]。

    Attributes:
        queries: list[str]，最终查询集（queries[0] 即主查询，供 reranker 打分）
        standalone: str，消解指代后的自包含查询
        degraded: bool，True 表示 LLM 失败已降级（queries 退化为 [原query]）
    """

    queries: list[str]
    standalone: str
    degraded: bool = False


def _finalize(original: str, out: RewriteOutput, *, max_query_chars: int,
              max_queries: int) -> list[str]:
    """把 LLM 输出整理成最终查询集：逐项过滤 → 大小写归一去重 → 生成侧截
    ``max_queries - 1`` → 原 query 兜底。

    Args:
        original: 用户原始 query（去空白后兜底进查询集）。
        out: 改写 LLM 的结构化输出。
        max_query_chars: 单条查询的字符上限，超限丢弃。
        max_queries: 最终查询集封顶条数（含原 query）。

    Returns:
        整理后的查询列表，永不为空（至少含原 query）。
    """
    seen: set[str] = set()
    generated: list[str] = []

    def add(item: Any) -> None:
        """过滤并登记一条候选查询。

        Args:
            item: LLM 输出中的单条候选（应为字符串，其余类型静默丢弃）。
        """
        if not isinstance(item, str):
            return
        text = item.strip()
        if not text or len(text) > max_query_chars:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        generated.append(text)

    add(out.standalone_query)
    # 先全量过滤再截席位：非法项不得占坑——若先截前 rewrite_num 条再过滤，
    # 前 rewrite_num 条全非法时会把排在后面的合法项挤出查询集
    for r in list(out.rewrites or []):
        add(r)
    # 生成侧最多占 max_queries-1 席，最后 1 席留给原 query（召回兜底，必在）
    generated = generated[:max_queries - 1]
    # 截席可能裁掉已登记项：用保留项重建 seen，否则与被裁项同文的原 query
    # 会被兜底去重误丢，违背「原 query 必在」
    seen = {q.casefold() for q in generated}
    original = original.strip()
    if original and original.casefold() not in seen:
        generated.append(original)
    return generated or [original or ""]


def _clean_history(history: Sequence | None, limit: int) -> list:
    """只留带 user/assistant 角色且有内容的消息，截最近 limit 条。

    limit <= 0 视为不喂历史（0 的语义是「不喂」），返回 []——否则
    ``[-limit:]`` 在 limit=0 时返回整段历史，语义恰好相反。

    Args:
        history: 对话历史消息序列（含 role/content 属性）。
        limit: 保留的最近条数上限。

    Returns:
        过滤并截断后的消息列表，输入无效时为空列表。
    """
    if not history or limit <= 0:
        return []
    kept = [m for m in history
            if getattr(m, "role", None) in ("user", "assistant")
            and (getattr(m, "content", None) or "").strip()]
    return kept[-limit:]


def _build_prompt(query: str, history: list, requirements: str) -> str:
    """拼改写 prompt：对话历史段 + 当前问题 + 要求段。

    Args:
        query: 用户当前问题。
        history: 已过滤的历史消息列表（可为空）。
        requirements: 改写要求段文本。

    Returns:
        完整 prompt 字符串。
    """
    lines: list[str] = []
    if history:
        lines.append("【对话历史】（仅用于理解指代与省略，不要检索其中的内容）")
        for m in history:
            role = "用户" if m.role == "user" else "助手"
            lines.append(f"{role}: {(m.content or '')[:HISTORY_MESSAGE_CHARS]}")
        lines.append("")
    lines.append(f"【当前问题】{query}")
    lines.append(requirements)
    return "\n".join(lines)


def _run_sync(coro):
    """把协程跑成同步：普通线程用 asyncio.run；已在事件循环内（异步宿主调用）
    时丢到工作线程执行——不能在运行中的 loop 里再 asyncio.run（嵌套 loop 崩溃）。

    Args:
        coro: 要同步执行的协程对象。

    Returns:
        协程的返回值。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class QueryRewriter:
    """检索查询改写器：一次结构化 LLM 调用产出 standalone + rewrites，失败降级原 query。

    Attributes:
        _history_limit: int，拼进改写 prompt 的最近历史条数
        _max_queries: int，最终查询集封顶（含原 query）
        _max_query_chars: int，单条改写查询的字符上限
        _requirements: str，按 rewrite_num 生成的 prompt 要求段
        _schema: type[BaseModel]，改写结构化输出模型
        _so: StructuredOutput，改写调用通道（解析失败零重试）
    """

    def __init__(self, llm, history_limit: int, *, rewrite_num: int,
                 max_queries: int, max_query_chars: int):
        """生产值 ``rag.query_rewrite.*``（唯一声明点 config.py）由 RAGService
        get_rewriter 注入；测试直构时显式传参（与 chunker 同一约定：服务类不自带默认值）。

        Args:
            history_limit: 拼进改写 prompt 的最近历史条数（condense 的输入窗口）。
            rewrite_num: prompt 要求的改写变体条数。
            max_queries: 最终查询集封顶（含原 query）。
            max_query_chars: 单条改写查询的字符上限（超限丢弃）。
        """
        from paperflow.core.llm.structured import StructuredOutput, StructuredOutputConfig
        self._history_limit = history_limit
        self._max_queries = max_queries
        self._max_query_chars = max_query_chars
        self._requirements = _build_requirements(rewrite_num)
        self._schema = _make_output_schema(rewrite_num)
        # REWRITE_MAX_RETRIES：解析失败零重试，失败延迟上限 = 1 次调用
        self._so = StructuredOutput(
            llm, StructuredOutputConfig(max_retries=REWRITE_MAX_RETRIES))

    def rewrite(self, query: str, history: Sequence | None = None) -> RewriteResult:
        """改写 query，返回最终查询集。任何异常降级为 [原query]（degraded=True）。

        Args:
            query: 用户当前问题。
            history: 对话历史消息序列，用于消解指代（可为 None）。

        Returns:
            改写结果；LLM 失败时为 [原query] 的降级结果。
        """
        try:
            out = _run_sync(self._so.extract(
                _build_prompt(query, _clean_history(history, self._history_limit),
                              self._requirements),
                self._schema))
            queries = _finalize(query, out, max_query_chars=self._max_query_chars,
                                max_queries=self._max_queries)
            if not queries or queries == [""]:
                raise ValueError("改写输出整理后为空")
            return RewriteResult(queries=queries, standalone=queries[0])
        except Exception as e:
            logger.warning("query 改写失败，降级为原始 query 检索：%s", e)
            return RewriteResult(queries=[query], standalone=query, degraded=True)
