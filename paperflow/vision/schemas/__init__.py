"""vision 域的数据模型（跨段共享），一模型一文件。

两段式管线以这里的模型衔接：`page.py`/`document_layout.py`/`caption.py` 是**解析段**
的产出（一页文本、文档级版面统计、图注），`figure.py` 是**提取段**的产物、
`analysis.py` 是**视觉模型段**的产物。它们都被下游多段直接读取，故集中声明、与
产出它们的实现文件解耦（此前 `Page` 住在 `parsers/text_extractor.py`，取一个类型会
连带拉起 PyMuPDF）。本包只做再导出，消费方从 `paperflow.vision.schemas` 取。
"""

from .analysis import FigureAnalysis
from .caption import Caption, CaptionParagraph, CaptionStart
from .document_layout import DocumentLayout
from .figure import Figure
from .page import Page

__all__ = [
    "Figure",
    "FigureAnalysis",
    "Page",
    "DocumentLayout",
    "Caption",
    "CaptionParagraph",
    "CaptionStart",
]
