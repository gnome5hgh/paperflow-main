"""回答质量评测：检索+生成 harness 的端到端 judge（忠实度 / 答题相关性 / 引用校验）。

与检索评测（同目录 `evaluation.py`，hit_rate/MRR）互补：那边评「找得准不准」，
这边评「答得好不好」。指标机制照搬 RAGAS：忠实度=答案拆陈述句逐条对证检索
上下文（陈述级二值判定比整体打分稳）；答题相关性=看答案反向生成候选问题、
与原题算余弦相似度。引用校验是纯代码——答案里标注的 [来源:…] 是否真的出现
在检索块中，宁漏判不误判。

judge 载体是 core/llm 的 StructuredOutput（temperature=0 + JSON 校验重试）。
judge 与被测生成共用同一 LLM——存在「自我偏好」风险，缓解手段是二值判定 +
抽小样本人丁校准。
"""
import asyncio
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from paperflow.core.llm import Message, StructuredOutput

#: 答案中的引用标注：[来源:<标识>]。<标识> 取自所用检索段落的首行
#: （论文标题或「标题 > 章节」的章节部分），判卷时在其块文本/路径里做子串核对
_CITATION_RE = re.compile(r"\[来源[:：]\s*([^\]]+)\]")


class Statements(BaseModel):
    """忠实度第一步的产出：答案拆成的事实陈述句列表。"""
    statements: list[str] = Field(
        description="答案中的事实陈述句列表，每条只含一个可独立核对的事实，保留原文表述")


class StatementVerdict(BaseModel):
    """单条陈述的判定。"""
    statement: str = Field(description="对应输入陈述的原文")
    supported: bool = Field(description="该陈述是否能在上下文中找到直接依据")
    reason: str = Field(description="一句话理由，引用上下文原文片段")


class FaithfulnessVerdict(BaseModel):
    """忠实度第二步的产出：全部陈述的批量判定（顺序与输入一致）。"""
    verdicts: list[StatementVerdict] = Field(description="每条陈述的判定")


class ReverseQuestions(BaseModel):
    """答题相关性的中间产出：这段答案最可能回答的候选问题。"""
    questions: list[str] = Field(description="恰好 3 个候选问题，彼此覆盖答案的不同侧面")


def check_citations(answer: str, chunks: list[tuple[str, str]],
                    ) -> tuple[float | None, bool, list[dict]]:
    """核对答案中的 [来源:…] 引用是否真实出现在检索块里（纯代码，零 LLM）。

    判定规则（宁漏判不误判）：引用内文去首尾空白、转小写后，在任一块的
    text 或 path 中做大小写不敏感的子串匹配；匹配不到一律判 not supported——
    校验器高估比低估危害大，模糊情形留给 per_item 明细供人工复核。

    Args:
        answer: 生成的答案原文。
        chunks: 检索命中块，每项 (text, path)。

    Returns:
        (accuracy, has_citations, details)：accuracy = 判对数/总引用数，
        无引用时 (None, False, [])；details 每项 {"citation", "supported"}。
    """
    citations = [m.group(1).strip() for m in _CITATION_RE.finditer(answer)]
    if not citations:
        return None, False, []
    haystacks = []
    for text, path in chunks:
        haystacks.append(text.lower())
        haystacks.append(path.lower())
    details = []
    hit = 0
    for c in citations:
        supported = bool(c.lower()) and any(c.lower() in h for h in haystacks)
        details.append({"citation": c, "supported": supported})
        hit += 1 if supported else 0
    return hit / len(citations), True, details


# ── 生成 harness 与忠实度判卷 ──

#: 生成侧的 system prompt：只依据检索段落作答 + 引用格式约定 + 长度不计分声明
_GENERATE_SYSTEM = (
    "你是学术研究问答助手。严格依据给定的检索段落回答问题；关键论断后标注来源，"
    "格式为 [来源:<标识>]，<标识> 取自所用段落的首行（论文标题，或「标题 > 章节」"
    "中的章节部分）。检索段落不足以回答时如实说明。直接作答，不要刻意扩展篇幅。"
)

#: 判卷声明（长度/格式偏见的最稳定缓解：白纸黑字写进判卷 prompt）
_JUDGE_NEUTRALITY = "长度与格式不计入判定。"

#: 忠实度第一步的判卷 prompt 模板：把答案拆成独立事实陈述句
_SPLIT_PROMPT = (
    "把下面的答案拆成独立的事实陈述句。要求：每条只含一个可核对的事实；"
    "保留原文表述，不要改写或推断；省略寒暄、过渡与重复。\n\n"
    "答案：\n{answer}")

#: 忠实度第二步的判卷 prompt 模板：对每条陈述做二值支持判定。
#: 模板整段统一用 f-string 定义：`{_JUDGE_NEUTRALITY}` 在模块加载时插值，
#: `{{context}}`/`{{statements_json}}` 的双花括号经 f-string 折叠为单花括号，
#: 供运行时 `.format()` 替换。占位符必须全部写在 f-string 段——若某段漏了
#: f 前缀，其双花括号会原样存活到 `.format()` 时刻，被当转义序列输出为
#: 字面量 `{statements_json}`，判卷模型将看不到待判陈述列表。
_VERDICT_PROMPT = (
    "逐条判断每条陈述是否被上下文直接支持。判据：\n"
    "1. 陈述中的事实能在上下文中找到原文依据 → supported=true；\n"
    "2. 上下文只部分覆盖、或需要推理/常识补全才能成立 → supported=false；\n"
    "3. 上下文与陈述矛盾 → supported=false。\n"
    f"{_JUDGE_NEUTRALITY}\n\n上下文：\n{{context}}\n\n陈述列表（JSON）：\n"
    f"{{statements_json}}")


def _build_context(chunks: list[tuple[str, str]]) -> str:
    """把检索块拼成判卷/生成用的上下文文本（保留首行前缀，编号便于引用）。"""
    return "\n\n".join(f"[段落{i + 1}] {text}\n(路径: {path})"
                       for i, (text, path) in enumerate(chunks))


@dataclass
class ItemResult:
    """单题评测结果：聚合的输入单元，也是 judge 人工校准的原料。

    status 取值：ok（全流程成功）/ judge_failed（判卷失败，指标留空但
    生成与引用校验仍有效）/ no_context（检索无命中）/ generation_failed
    （生成抛错）。低质量指标靠 per_item 明细人工复核，故各中间产物原样保留。
    """
    query: str
    status: str
    answer: str | None = None
    contexts: list[str] | None = None
    faithfulness: float | None = None
    n_statements: int | None = None
    n_supported: int | None = None
    answer_relevancy: float | None = None
    citation_accuracy: float | None = None
    citation_coverage: bool = False
    citations_detail: list[dict] = field(default_factory=list)
    verdicts: list[dict] = field(default_factory=list)


async def evaluate_item(query: str, *, retrieve, llm, judge: StructuredOutput,
                        embedder, top_k: int = 5) -> ItemResult:
    """评一道题：检索 → 生成 → 忠实度判卷 → 引用校验 →（后续）相关性判卷。

    retrieve 是同步函数（RAGService.retrieve 持锁跑推理与查询），经 to_thread
    调用避免阻塞事件循环；判卷的 StructuredOutput 校验失败走 fallback（置
    judge_failed，聚合时剔除）而非抛错——单题失败不该拖垮整批。

    Args:
        query: 被评测的问题。
        retrieve: 同步检索函数，签名 (query, top_k) -> list[(text, path)]。
        llm: 生成侧 LLM 客户端（自由文本，不走结构化）。
        judge: 结构化判卷载体，负责拆句与批量判定两次调用。
        embedder: 相关性判卷用的嵌入器，当前判卷流程暂未使用，预留接口。
        top_k: 检索命中数上限。

    Returns:
        ItemResult：status 反映该题走到哪一步，指标字段按可达阶段填充。
    """
    chunks = await asyncio.to_thread(retrieve, query, top_k)
    if not chunks:
        return ItemResult(query=query, status="no_context")

    context = _build_context(chunks)
    result = ItemResult(query=query, status="ok", contexts=[c for c, _ in chunks])

    # 生成（自由文本，不走结构化）：失败即整题无答案，提前返回
    try:
        message = await llm.chat(
            [Message(role="system", content=_GENERATE_SYSTEM),
             Message(role="user", content=f"检索段落：\n{context}\n\n问题：{query}")],
            temperature=0.0)
        result.answer = message.content
    except Exception:
        result.status = "generation_failed"
        return result

    # 引用校验（纯代码，先于判卷——judge 挂了它也有效）
    result.citation_accuracy, result.citation_coverage, result.citations_detail = \
        check_citations(result.answer, chunks)

    # 忠实度两步判卷（拆句 → 批量判定）；任一步失败整题记 judge_failed。
    # 批量判定带保守 fallback：判卷输出格式坏掉时不整题崩，而是全部判
    # 不支持并保留 reason，便于人工校准时识别这批降级样本。
    try:
        statements = await judge.extract(
            _SPLIT_PROMPT.format(answer=result.answer), Statements)
        verdict_prompt = _VERDICT_PROMPT.format(
            context=context,
            statements_json=statements.model_dump_json())
        verdict = await judge.extract(
            verdict_prompt, FaithfulnessVerdict,
            fallback=lambda: FaithfulnessVerdict(verdicts=[
                StatementVerdict(statement=s, supported=False,
                                 reason="judge 输出校验失败，保守判不支持")
                for s in statements.statements]))
        result.verdicts = [v.model_dump() for v in verdict.verdicts]
        result.n_statements = len(verdict.verdicts)
        result.n_supported = sum(1 for v in verdict.verdicts if v.supported)
        result.faithfulness = (result.n_supported / result.n_statements
                               if result.n_statements else None)
    except Exception:
        result.status = "judge_failed"

    return result
