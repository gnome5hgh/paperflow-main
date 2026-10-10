# paperflow/core/common/text.py
"""信任边界文本清洗工具。

与内容扫描（scanner.py）同属"信任边界输入清洗"：在外部输入进入模型前统一
做编码清洗与威胁扫描。本模块的唯一职责是清洗未配对的 surrogate 字符。

为什么需要：PDF 提取（PyMuPDF）或外部文本可能携带未配对的
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
#: 涵盖 U+D800–U+DFFF 全部高低代理位，用于快速检测文本是否包含非法码点。
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text: str) -> str:
    """把文本里的未配对 surrogate 清洗为合法标量：优先无损还原，失败降级 U+FFFD。

    对正常文本零开销返回原串（search 无匹配即返回原对象）。含 surrogate 的脏文本
    先尝试 surrogateescape 回环——把 U+DC80-U+DCFF 代理序列还原回原始 UTF-8 字节
    再严格解码，完整序列可无损恢复原字符（surrogateescape 残留场景，如终端/PDF
    输入逐字节解码损坏）；字节序列非法（孤立/截断代理）时降级为 U+FFFD 替换。
    保证下游向量化编码、发给大模型、SQL 落盘不会因非法字符崩溃。

    Args:
        text: 待清洗的输入字符串（可能包含非法 surrogate 码点）

    Returns:
        str: 清洗后的字符串，所有非法 surrogate 已被替换为合法字符（原字符或 �）

    算法思路：
        1. 快速路径：空字符串或未检测到 surrogate 时直接返回原对象（零开销）。
        2. 尝试 ``encode('utf-8', 'surrogateescape').decode('utf-8')`` 做无损回环。
           原理：Python 的 surrogateescape 错误处理器会将 U+DC80–U+DCFF 范围内的
           代理码点映射回对应的单字节（0x80–0xFF），这些字节通常是 UTF-8 多字节序列
           被错误解码为代理的残留；随后用严格解码器重新解码这些字节，可以还原原始字符。
           例如，一个 UTF-8 编码的 'é'（0xC3 0xA9）若被错误地以 Latin-1 解码，会变成
           U+00C3 和 U+00A9，再被编码成 UTF-8 时可能产生 0xC3 0xA9 的代理残留；
           surrogateescape 能将其反转回正确的字节序列，从而实现无损还原。
        3. 如果上述回环失败（说明存在孤立代理或截断的字节序列），则用替换字符（�）
           直接替换所有 surrogate 码点，保证输出始终合法。

    边界条件与注意事项：
        - 该函数假设输入是 str 类型；调用方应确保传入字符串而非 bytes。
        - 对于完全正常的文本，正则搜索是 O(n) 但 Python 会快速跳过，开销极低。
        - surrogateescape 回环仅对“残留”场景有效（即非法代理由 Python 的
          surrogateescape 解码器产生），对于人工构造的孤立代理（如单个 \\uD800），
          encode 阶段会因无法映射到字节而抛出 UnicodeEncodeError，触发降级替换。
        - 降级替换使用 U+FFFD（�），这是 Unicode 官方推荐的替换字符，下游可安全处理。
    """
    # 快速路径：空字符串或没有 surrogate 则直接返回原对象（零拷贝）
    if not text:
        return text
    if not _SURROGATE_RE.search(text):
        return text
    try:
        # 尝试无损回环：将 surrogate 码点还原为原始 UTF-8 字节，再严格解码
        return text.encode("utf-8", "surrogateescape").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # 回环失败（例如存在无法映射到字节的孤立代理 \uD800）：
        # 使用替换字符逐个替代所有未配对 surrogate
        return _SURROGATE_RE.sub("�", text)
