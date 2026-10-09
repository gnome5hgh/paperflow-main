"""引用键的生成规则：`{一作姓氏}{年份}{短标题}`。

对齐手写文献库的约定，确保 key 人类可读且稳定。纯函数、无 I/O，与文件读写
（storage/）解耦——同一套规则既用于现场生成预备 key，也用于校验既有条目。
"""
from __future__ import annotations

import re

#: key 生成的短标题停用词（首词过滤；其余一律保留）
#: 例如 "The Attention Mechanism" → "attention"（过滤 "the"）。
_STOPWORDS = {"the", "a", "an", "of", "in", "for", "and", "on", "to", "with",
              "toward", "towards", "from", "by", "at", "using", "via"}


def _shorttitle(title: str) -> str:
    """从全标题提取用于生成 key 的短标题（首个非停用词）。

    算法：
        1. 用正则提取所有字母数字词（过滤标点）。
        2. 返回第一个不在 _STOPWORDS 中的词。
        3. 若所有词均为停用词，则回退返回第一个词；若无词则返回 "paper"。

    边界：标题 "A Study of ..." → 停用词 "a" 被过滤，返回 "study"。

    Args:
        title: str，论文全标题

    Returns:
        用于生成 key 的短标题（首个非停用词；全为停用词时取首词，无词则 "paper"）。
    """
    words = re.findall(r"[A-Za-z0-9]+", title.lower())
    for w in words:
        if w not in _STOPWORDS:
            return w
    return words[0] if words else "paper"


def gen_key(title: str, authors: str, year: str) -> str:
    """生成 `{firstauthor}{year}{shorttitle}` BibTeX key。

    对齐手写库约定，确保人类可读且稳定。
    例如：作者 "John Smith"、年份 "2024"、标题 "Attention Is All You Need"
          → "smith2024attention"（忽略 "is/all/you/need" 等停用词）。

    Args:
        title: 论文全标题。
        authors: 作者字符串（如 "Smith, John and Doe, Jane"），
                 只取第一个作者（按逗号或空格分割）。
        year: 发表年份（字符串）。

    Returns:
        生成的 key（小写）。
    """
    first = ""
    if authors:
        first = re.split(r"[,\s]+", authors.strip())[0].lower()
    return f"{first}{year}{_shorttitle(title)}"
