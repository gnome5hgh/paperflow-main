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
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

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
    haysticks = []
    for text, path in chunks:
        haysticks.append(text.lower())
        haysticks.append(path.lower())
    details = []
    hit = 0
    for c in citations:
        supported = bool(c.lower()) and any(c.lower() in h for h in haysticks)
        details.append({"citation": c, "supported": supported})
        hit += 1 if supported else 0
    return hit / len(citations), True, details
