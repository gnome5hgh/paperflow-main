"""citation-agent 的工具装配：引用管理全家桶 + 读 PDF，不给文件写工具。

文献库维护 agent：bib 的所有写入经 CitationManager（追加/删除/同步都是
托管操作），不需要裸文件工具；不装配 spawn（叶子 agent 不递归调度）、
不装配 RAG 检索（不读论文内容做分析）。read_pdf 用于元数据缺失时读首页
取作者/年份（读元数据不属于「读论文内容做分析」），只要标题则用更轻的
extract_title。
删除目标不明确时不要猜：把候选与缺口写进结果交上级定夺（删除动作本身仍由
策略引擎向用户逐次确认，与提问无关）。
"""
from paperflow.citations import CitationManager
from paperflow.config import PaperFlowConfig
from paperflow.tools.citations import (LookupCitationTool, AddCitationTool,
                                       FormatCitationsTool, ListCitationsTool,
                                       SyncCitationsTool, RemoveCitationTool)
from paperflow.tools import ExtractTitleTool, ReadPdfTool
from paperflow.tools.common.factory import make_tools


_cm = CitationManager(PaperFlowConfig.from_env())

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    LookupCitationTool(_cm), AddCitationTool(_cm),
    FormatCitationsTool(_cm), ListCitationsTool(_cm),
    SyncCitationsTool(_cm), RemoveCitationTool(_cm),
    ExtractTitleTool,                       # 只要标题时走轻路径，不必读整篇
    ReadPdfTool,                            # 元数据缺失时读首页取作者/年份
])
