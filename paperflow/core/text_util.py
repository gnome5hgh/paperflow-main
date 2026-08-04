# paperflow/core/text_util.py
"""文本清洗工具。

目前唯一职责：清洗未配对 surrogate 字符（U+D800-U+DFFF）。

为什么需要：PDF 提取（GROBID TEI / PyMuPDF 回退）或外部文本可能携带未配对的
surrogate（孤立的高/低代理位），它们不是合法 Unicode 标量值，会在两处爆炸：
- ``BgeEmbedder.encode`` → tokenizer 抛 ``TypeError: TextEncodeInput must be
  Union[...]``（意图路由整条降级）；
- openai SDK 把消息 UTF-8 编码发给 LLM → 抛 ``UnicodeEncodeError:
  surrogates not allowed``（整轮 ReAct 崩溃）。

在信任边界（embed 输入 / LLM 消息出站）统一清洗，保证任何来源的脏文本都被
兜住；用 U+FFFD 替换而不是删除，保留字符位置语义。
"""

import re

#: 未配对 surrogate 区间（UTF-16 代理对专用，合法标量值不含此区间）
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text: str) -> str:
    """把文本里的未配对 surrogate 替换为 U+FFFD（replacement char）。

    对正常文本零开销返回原串（re.sub 无匹配时返回原对象）；对含 surrogate 的
    脏文本就地替换，保证下游 encode/LLM 编码不炸。
    """
    if not text:
        return text
    return _SURROGATE_RE.sub("�", text)
