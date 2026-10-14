"""产物语义根名的词表。

`_root_map`（语义根名 → 绝对路径）的键，也是工具 `root_hints` 唯一合法的取值：
写成别的字面量不会报错，只会让 description 里的 `[目录]` 提示行**静默消失**
（`make_tools` 遇到不认识的名字直接跳过），模型于是看不到语料库位置。
"""

from enum import StrEnum

__all__ = ["ScopeRoot"]


class ScopeRoot(StrEnum):
    """工具的语义根名。"""

    #: 笔记根
    NOTE = "note"
    #: 论文 PDF 语料根
    PDF = "pdf"
    #: 选题与研究计划根
    RESEARCH = "research"
    #: 记忆块投影根（MemFS）
    MEMORY = "memory"
    #: 草稿根（路径对模型不透明，故 `[目录]` 提示会跳过它）
    SCRATCH = "scratch"
