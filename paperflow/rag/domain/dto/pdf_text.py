"""PDF 文本 dto：本地解析一篇 PDF 的完整产出。

``blocks`` 是唯一真相源——它既给出正文，也给出每块在原页上的位置（索引侧据此给
检索块标注页码与坐标）；``body`` 只是它的投影，不单独存一份以免两处不一致。
"""
from dataclasses import dataclass

from paperflow.rag.domain.dto.block import Block


@dataclass(frozen=True)
class PdfText:
    """一个 PDF 的本地抽取结果。

    Attributes:
        title: 论文标题；元数据与首页启发式都拿不到时为空串。
        pages: 页数。
        blocks: 正文按渲染块拆开的明细（阅读顺序）。
    """

    title: str
    pages: int
    blocks: tuple[Block, ...] = ()

    @property
    def body(self) -> str:
        """正文 markdown 文本（各渲染块按空行连接）。"""
        return "\n\n".join(b.text for b in self.blocks)
