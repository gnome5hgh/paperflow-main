# paperflow/rag/services/query_rewriter.py
"""QueryRewriter：检索前对 query 做一次 LLM 改写（condense + multi-query，spec 2026-10-04）。

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

from paperflow.rag.constants import (
    HISTORY_MESSAGE_CHARS,
    HISTORY_MESSAGES,
    MAX_QUERIES,
    MAX_QUERY_CHARS,
    REWRITE_MAX_RETRIES,
    REWRITE_NUM,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是学术问答系统的检索查询改写器。根据对话历史（若有）与当前问题，"
    "输出用于知识库检索的查询集合。"
)

_REQUIREMENTS = f"""
【要求】
1. standalone_query：把当前问题改写为自包含的检索查询——消解指代、补全省略，不依赖上文也能看懂；保持与当前问题相同的主要语言。
2. rewrites：给出 {REWRITE_NUM} 条与 standalone_query 语义等价但措辞不同的检索查询，覆盖同义替换、关键词化、中英术语互补等角度；每条独立可检索，不要加解释。
3. 查询贴合学术语料（论文正文、读书笔记）的措辞，不要口语化表达。
"""


class RewriteOutput(BaseModel):
    """改写 LLM 的结构化输出 schema（同时经 _schema_to_prompt 展开成输出模板）。"""

    standalone_query: str = Field(description="消解指代后的自包含检索查询，保持原问题主语言")
    rewrites: list[str] = Field(
        description=f"{REWRITE_NUM} 条语义等价但措辞不同的检索查询")


@dataclass
class RewriteResult:
    """改写结果：queries 是最终查询集（queries[0] 即主查询，供 reranker 打分）。

    degraded=True 表示 LLM 失败已降级——queries 退化为 [原query]。
    """

    queries: list[str]
    standalone: str
    degraded: bool = False


def _finalize(original: str, out: RewriteOutput) -> list[str]:
    """把 LLM 输出整理成最终查询集：逐项过滤 → 大小写归一去重 → 生成侧截 3 → 原 query 兜底。"""
    seen: set[str] = set()
    generated: list[str] = []

    def add(item: Any) -> None:
        if not isinstance(item, str):
            return
        text = item.strip()
        if not text or len(text) > MAX_QUERY_CHARS:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        generated.append(text)

    add(out.standalone_query)
    # 先全量过滤再截席位：非法项不得占坑——若先截 REWRITE_NUM 条再过滤，
    # 前 3 条全非法时会把排在后面的合法项挤出查询集
    for r in list(out.rewrites or []):
        add(r)
    # 生成侧最多占 MAX_QUERIES-1 席，最后 1 席留给原 query（召回兜底，必在）
    generated = generated[:MAX_QUERIES - 1]
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
    """
    if not history or limit <= 0:
        return []
    kept = [m for m in history
            if getattr(m, "role", None) in ("user", "assistant")
            and (getattr(m, "content", None) or "").strip()]
    return kept[-limit:]


def _build_prompt(query: str, history: list) -> str:
    lines: list[str] = []
    if history:
        lines.append("【对话历史】（仅用于理解指代与省略，不要检索其中的内容）")
        for m in history:
            role = "用户" if m.role == "user" else "助手"
            lines.append(f"{role}: {(m.content or '')[:HISTORY_MESSAGE_CHARS]}")
        lines.append("")
    lines.append(f"【当前问题】{query}")
    lines.append(_REQUIREMENTS)
    return "\n".join(lines)


def _run_sync(coro):
    """把协程跑成同步：普通线程用 asyncio.run；已在事件循环内（异步宿主调用）
    时丢到工作线程执行——不能在运行中的 loop 里再 asyncio.run（嵌套 loop 崩溃）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class QueryRewriter:
    """检索查询改写器：一次结构化 LLM 调用产出 standalone + rewrites，失败降级原 query。"""

    def __init__(self, llm, history_limit: int | None = None):
        from paperflow.core.llm.structured import StructuredOutput, StructuredOutputConfig
        # history_limit：拼进改写 prompt 的最近历史条数，默认 HISTORY_MESSAGES=6；
        # RAGService.get_rewriter 传 rag.query_rewrite.history_messages（改 YAML 即生效）。
        self._history_limit = HISTORY_MESSAGES if history_limit is None else history_limit
        # REWRITE_MAX_RETRIES=0：解析失败零重试（spec §6 与主流对齐），失败延迟上限 = 1 次调用
        self._so = StructuredOutput(
            llm, StructuredOutputConfig(max_retries=REWRITE_MAX_RETRIES))

    def rewrite(self, query: str, history: Sequence | None = None) -> RewriteResult:
        """改写 query，返回最终查询集。任何异常降级为 [原query]（degraded=True）。"""
        try:
            out = _run_sync(self._so.extract(
                _build_prompt(query, _clean_history(history, self._history_limit)),
                RewriteOutput))
            queries = _finalize(query, out)
            if not queries or queries == [""]:
                raise ValueError("改写输出整理后为空")
            return RewriteResult(queries=queries, standalone=queries[0])
        except Exception as e:
            logger.warning("query 改写失败，降级为原始 query 检索：%s", e)
            return RewriteResult(queries=[query], standalone=query, degraded=True)
