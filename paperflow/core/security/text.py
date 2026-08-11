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

在信任边界（向量化输入 / 发往大模型的消息 / 消息与审计落盘）统一清洗，保证
任何来源的脏文本都被兜住；优先 surrogateescape 回环无损还原（surrogateescape
残留是完整字节序列，可还原回原字符），无法还原的孤立代理才用 U+FFFD 替换。
"""
import re

#: 未配对 surrogate 区间（UTF-16 代理对专用，合法标量值不含此区间）
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text: str) -> str:
    """把文本里的未配对 surrogate 清洗为合法标量：优先无损还原，失败降级 U+FFFD。

    对正常文本零开销返回原串（search 无匹配即返回原对象）。含 surrogate 的脏文本
    先尝试 surrogateescape 回环——把 U+DC80-U+DCFF 代理序列还原回原始 UTF-8 字节
    再严格解码，完整序列可无损恢复原字符（surrogateescape 残留场景，如终端/PDF
    输入逐字节解码损坏）；字节序列非法（孤立/截断代理）时降级为 U+FFFD 替换。
    保证下游向量化编码、发给大模型、SQL 落盘不会因非法字符崩溃。
    """
    if not text:
        return text
    if not _SURROGATE_RE.search(text):
        return text
    try:
        # surrogateescape 回环：每个代理还原为其字节，再严格解码成原字符
        return text.encode("utf-8", "surrogateescape").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # 孤立/截断的代理（如 0xE4 单独残留）不是完整字节序列 → 无法还原，降级
        return _SURROGATE_RE.sub("�", text)
