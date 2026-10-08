"""RAG 域的跨模块词汇。

source 随每个检索块写入向量库的 source 列，并进 LLM 可见的工具 schema 与检索
结果展示——改值会同时动存储与工具契约，不能只改一头。枚举在 `enums.py`；本包
只做再导出，消费方一律从 `paperflow.rag.constants` 取。
"""

from .enums import RagSource

__all__ = ["RagSource"]
