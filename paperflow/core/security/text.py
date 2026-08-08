# paperflow/core/security/text.py
"""信任边界文本清洗工具。

与内容扫描（scanner.py）同属"信任边界输入清洗"：在外部输入进入模型前统一
做编码清洗与威胁扫描。本模块的唯一职责是清洗未配对的 surrogate 字符。

为什么需要：PDF 提取（如 GROBID、PyMuPDF）或外部文本可能携带未配对的
surrogate（孤立的高/低代理位），它们不是合法的 Unicode 标量值，会让下游
两处崩溃：
- 向量化编码：tokenizer 抛 ``TypeError: TextEncodeInput must be
  Union[...]``，导致语义检索整条链路降级；
- 发送给大模型：openai SDK 把消息按 UTF-8 编码时会抛 ``UnicodeEncodeError:
  surrogates not allowed``，整轮对话崩溃。

在信任边界（向量化输入 / 发往大模型的消息）统一清洗，保证任何来源的脏文本
都被兜住；用 U+FFFD 替换而不是删除，保留字符位置语义。
"""
import re

#: 未配对 surrogate 区间（UTF-16 代理对专用，合法标量值不含此区间）
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text: str) -> str:
    """把文本里的未配对 surrogate 替换为 U+FFFD（replacement char）。

    对正常文本零开销返回原串（re.sub 无匹配时返回原对象）；对含 surrogate 的
    脏文本就地替换，保证下游向量化编码、发给大模型时不会因非法字符崩溃。
    """
    if not text:
        return text
    return _SURROGATE_RE.sub("�", text)
