"""检索块实体：切块器的产出，也是索引与检索共同的领域对象。

块 id 由「绝对路径 + 块序号」哈希而来、与内容无关，同一位置重切得到同一个 id，
索引侧的「先删后建」依赖这一稳定性。块正文与「进编码的文本」是两件事：后者由
``indexed_text`` 在使用点现拼前缀，库内只存干净正文。
"""
from dataclasses import dataclass

from paperflow.rag.constants import CHUNK_TYPE_TEXT


def context_prefix(title: str, heading: str, body: str) -> str:
    """把「论文标题 > 章节标题」前缀行拼到块正文前。

    前缀随块文本同时进入 embedding、BM25 与检索展示（Anthropic contextual
    retrieval 的标题路径版：任一块被单独检回时都自带所属论文与章节）。
    标题与章节都缺省时返回原正文（笔记与无标题文档兼容）。

    Args:
        title: 文档标题（可为空串）。
        heading: 章节标题（可为空串）。
        body: 块正文文本。

    Returns:
        拼好前缀的块文本；标题与章节均缺省时为原正文。
    """
    label = " > ".join(x for x in (title.strip(), heading.strip()) if x)
    return f"{label}\n{body}" if label else body


def section_label(chunk: "Chunk") -> str:
    """块的「章节位」：章节名，媒体块则是表注/图注原文。

    文本块的章节在 `heading`；媒体块没有章节名、注文在 `caption`——两者在块前缀与
    检索结果的章节列里是**同一个位置**，取值规则只此一处。

    Args:
        chunk: 检索块。

    Returns:
        str: 章节名或注文；两者皆空时为空串。
    """
    return chunk.heading or chunk.caption


def indexed_text(chunk: "Chunk") -> str:
    """块进 embedding 与 BM25 时使用的文本：正文 + 「标题 > 章节位」前缀。

    库里存的是干净正文（见 `Chunk.text`），前缀在使用点现拼——它是派生的展示视图，
    改前缀规则不必重写库。索引侧编码与检索侧 BM25 重建都必须用它，两处若各拼各的，
    同一个块的稠密向量与稀疏索引就会基于不同文本，检索质量静默下降。

    Args:
        chunk: 检索块。

    Returns:
        str: 带前缀的文本；无标题无章节位时即正文本身。
    """
    return context_prefix(chunk.title, section_label(chunk), chunk.text)


@dataclass
class Chunk:
    """一个检索块：由切块器产出，包含文本与它的全部元数据。

    Attributes:
        id: str，块唯一标识 ``sha1(绝对路径 + 块序号)[:16]``；与内容无关，同位置
            重复切分得到相同 id（写入幂等）。
        text: str，块正文，取值随块类型而定（**这是契约**，改它等于改检索质量）：
            文本块给正文段落；**表块给按版面几何重建出的 markdown 表格**（行列关系只
            存在于坐标里，拍平成一串词就问不出「哪一行的数值是多少」）；**图块恒为空串**
            ——图内文字零散、做检索信号价值低，图的检索内容在 `caption`（图注）里。
            **不含**「标题 > 章节」前缀——前缀由使用点现拼（见 `indexed_text`），
            所以它与被编码的文本并不相同，这是刻意设计。
        path: str，文档的**绝对路径**（兼作文档 id 与元数据）。
        title: str，文档标题（取不到为空串）。
        heading: str，所属章节标题（媒体块为空）。
        caption: str，表注/图注原文（媒体块用，文本块为空）。
        image_key: str，图表原图在对象存储里的**对象键**（媒体块且存图时有值，其余为空）。
            存键不存 URL/路径：换存储地址或前面加分发层都不需要重索引。
        chunk_type: str，块类型：``text`` / ``table`` / ``figure``。
        position: tuple[tuple[int, int, int, int, int], ...]，块覆盖到的区域，
            每项为 ``(页, left, right, top, bottom)``；同一章节切多窗时各窗共享
            该章节的区间（窗口级坐标要把坐标一路带进装窗，暂不做）。
        chunk_index: int，块在文档中的全局序号（从 0 起）。
    """

    id: str            # 块唯一标识符，由 `sha1(绝对路径 + chunk_index)[:CHUNK_ID_LEN]` 生成，
                       # 该 ID 与内容无关，同一文档位置重复切分得到相同 ID，编辑同一位置会得到同 id，保证了索引写入的幂等性（覆盖而非追加）。
    text: str          # 正文：文本块=正文段落，表块=markdown 表格，图块=恒空（契约见类 docstring）
    path: str          # 文档的绝对路径（同时用作文档 id 与元数据）
    title: str = ""    # 文档标题
    heading: str = ""  # 该块所属章节的标题（可能为空）。
    caption: str = ""  # 表注/图注原文（媒体块用，文本块为空）
    image_key: str = ""  # 图表原图的对象键（媒体块且存图时有值；文本块为空）
    chunk_type: str = CHUNK_TYPE_TEXT   # text | table | figure
    position: tuple = ()                 # 覆盖到的区域：(页, left, right, top, bottom) 元组序列
    chunk_index: int = 0   # 块在文档中的全局序号（从0开始），用于生成 ID。

    @property
    def page_num(self) -> tuple[int, ...]:
        """块覆盖到的页码：从 position 去重后升序取出。"""
        return tuple(sorted({p[0] for p in self.position}))

    @property
    def top(self) -> int:
        """块覆盖区域的最高点（y 最小值）；无位置信息时为 0。"""
        return min((p[3] for p in self.position), default=0)
