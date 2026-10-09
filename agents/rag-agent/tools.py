"""rag-agent 的工具装配：语料检索 + 索引读写 + 索引体检（RAG 一域的全部工具）。

- **检索** `rag_retrieve`：按 query 取回命中片段（含来源、路径与摘录），可按 source
  限定 note / pdf。这是「在语料里找材料」的唯一入口——其他角色要检索就派发本角色，
  一次派发可带多个检索式（本角色逐个调用工具，不为每个 query 各回一轮）。
- **写入** `index_paths` / `reindex_all`：入库与全量收敛。
- **体检** `index_status`：只读对照状态文件 / 向量库 / 关键词索引 / 语料根，报告
  已索引多少、有无幽灵块与未入库、配方是否一致、PDF 解析器分布。诊断权也在责任人手里——
  「索引好像不对」先跑它，再据结论决定要不要收敛。

不给文件读写工具（路径由调用方给出、存在性由入库工具自己报）、不给 glob/grep。
失败如实回报，是否请示用户由上级决定。也不装记忆工具：
检索与索引维护不产生需要沉淀的清单或历史。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools.rag import IndexPathsTool, IndexStatusTool, RagRetrieveTool, ReindexAllTool

# config 在 import 时构造（每进程静态、无副作用，对齐 make_tools 惯例）
TOOLS = make_tools(PaperFlowConfig.from_env(), [
    RagRetrieveTool, IndexPathsTool, ReindexAllTool, IndexStatusTool,
])
