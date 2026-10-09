"""引用域的数据模型（跨层共享）。

BibEntry 是存储层的产物、ResolvedCitation 是业务层的产物，但两者都是消费方
（工具层 / agent）直接读取的载体，故集中声明、与实现解耦。枚举在 constants/。
"""

from .bib import BibEntry
from .citation import ResolvedCitation

__all__ = ["BibEntry", "ResolvedCitation"]
