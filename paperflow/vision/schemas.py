"""视觉分析子包的数据模型：PDF 提取出的图表对象。"""
from dataclasses import dataclass


@dataclass
class Figure:
    """从 PDF 提取出的一个图表对象。

    Attributes:
        number: 图号（图注解析出的数字）。
        caption: 图注文本。
        page: 所在页码（1 起）。
        image_bytes: 图片字节（栅格原始格式或渲染 PNG）。
        mime: 图片 MIME（"image/png"/"image/jpeg"），用于 base64 data URL。
    """

    number: int
    caption: str
    page: int
    image_bytes: bytes
    mime: str
