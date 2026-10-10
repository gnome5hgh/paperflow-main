"""图注数据模型：候选起始行 + 完整图注段落 + 精简版图注。

三个都是解析、检测、编排三段共同读取的载体，与 `find_captions` 等算法分离。
"""
from dataclasses import dataclass

from paperflow.vision.common.geometry import Box, Line, Paragraph
from paperflow.vision.constants import FigureType


@dataclass(frozen=True)
class CaptionStart:
    """一个「可能是图注起始行」的候选。

    与 CaptionDetector.scala 的 CaptionStart 一致，过滤器筛选用到的格式特征
    （colon_match/period_match/all_caps_fig 等）全部做成属性：

    - line_end: 图号词是否占满行尾——"Figure 3." 是，而 "Figure 3 shows..." 不是。
    - paragraph_start: 是否为所在段落的首行（保底消歧用的弱信号）。

    Attributes:
        header: str，图注起始词（如 Figure / Fig. / TABLE）
        name: str，图号（如 1 / 3.1 / III）
        fig_type: FigureType，图注类型（Figure / Table）
        number_syntax: str，图号后的分隔符：":" / "." / ""（行尾）
        line: Line，起始行对象
        next_line: Line | None，同段内的下一行（左对齐检查用）
        page: int，页码（0 起）
        paragraph_start: bool，该行是否为其所在段落的首行
        line_end: bool，图号词是否占满行尾（后面无其他词）
    """

    header: str          # 图注起始词（如 "Figure", "Fig.", "TABLE"）
    name: str            # 图号（如 "1", "3.1", "III"）
    fig_type: FigureType # 类型：Figure 或 Table
    number_syntax: str   # 图号后的分隔符：":" 或 "." 或 ""（行尾）
    line: Line           # 起始行对象
    next_line: Line | None  # 同一段内的下一行（用于左对齐检查）
    page: int            # 页码（0-based）
    paragraph_start: bool # 该行是否为其所在段落的首行
    line_end: bool       # 图号词是否占满行尾（即后面无其他词）

    @property
    def colon_match(self) -> bool:
        """图号后是否跟冒号。"""
        return self.number_syntax == ":"

    @property
    def period_match(self) -> bool:
        """图号后是否跟句点。"""
        return self.number_syntax == "."

    @property
    def all_caps_fig(self) -> bool:
        """起始词是否全大写 FIG 形式。"""
        return self.header.startswith("FIG")

    @property
    def all_caps_table(self) -> bool:
        """起始词是否为全大写 TABLE。"""
        return self.header == "TABLE"

    @property
    def fig_abbreviated(self) -> bool:
        """起始词是否为缩写 Fig.。"""
        return self.header == "Fig."



@dataclass(frozen=True)
class CaptionParagraph:
    """一页内的一个完整图注段落（起始行 + 扩展出的后续行）。

    Attributes:
        name: str，图号
        fig_type: FigureType，图注类型
        page: int，页码
        paragraph: Paragraph，构成该图注的段落
        boundary: Box，派生：段落外接矩形
        text: str，派生：段落全文
    """

    name: str
    fig_type: FigureType
    page: int
    paragraph: Paragraph

    @property
    def boundary(self) -> Box:
        """图注段落的外接矩形。"""
        return self.paragraph.boundary

    @property
    def text(self) -> str:
        """图注段落的完整文本。"""
        return self.paragraph.text



@dataclass(frozen=True)
class Caption:
    """精简版图注：正文文本 + 边界，供下游（FigureDetector 的失败图注）使用。

    Attributes:
        fig_type: FigureType，图注类型
        name: str，图号
        page: int，页码
        text: str，图注正文
        boundary: Box，图注边界
    """

    fig_type: FigureType
    name: str
    page: int
    text: str
    boundary: Box

    @classmethod
    def from_paragraph(cls, caption_paragraph: CaptionParagraph) -> "Caption":
        """从 CaptionParagraph 派生精简版（对应 Figure.scala 的 Caption.apply）。

        Args:
            caption_paragraph: CaptionParagraph，完整图注段落

        Returns:
            由该段落派生的精简版 Caption。
        """
        return cls(
            caption_paragraph.fig_type,
            caption_paragraph.name,
            caption_paragraph.page,
            caption_paragraph.text,
            caption_paragraph.boundary,
        )

