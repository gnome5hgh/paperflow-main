"""引用域的业务层：编排门面、语料索引、key 生成规则。

消费方（引用工具 / agent）只从 `paperflow.citations` 包级门面进入；本包内部
按角色分工——manager 编排、corpus 维护语料投影、keys 生成引用键。
"""

from .corpus import CorpusIndex
from .keys import gen_key
from .manager import CitationManager

__all__ = ["CitationManager", "CorpusIndex", "gen_key"]
