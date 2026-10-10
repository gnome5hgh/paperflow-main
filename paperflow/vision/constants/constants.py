"""vision 域的跨文件共享常量。

行宽分桶粒度是**切分与分类两处共同的结构契约**：`document_layout` 按它算标准行宽，
`region_classifier` 按它做行宽比对；改一处而另一处没跟，区域分类会静默错判。
"""
__all__ = ["LINE_WIDTH_BUCKET_SIZE"]

#: 行宽分桶粒度(pt)：把相近行宽聚到 2pt 桶里，弱化浮动对象/公式造成的宽度噪声
LINE_WIDTH_BUCKET_SIZE = 2
