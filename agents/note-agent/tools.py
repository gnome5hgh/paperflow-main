"""note-agent 的工具装配:原子文件工具 + 派发 review-agent 的审稿 spawn。

装配 read_file/read_pdf/write_file/edit_file/delete_file 五个原子工具(复用
paperflow/tools/ 的集中式安全边界与风险语义)、glob/grep 定位工具,以及
SpawnSubAgentTool——AGENT.md 的审稿循环用它派发 review-agent 子 agent 审阅草稿,
拿回裁决后经 edit_file 修订。
delete_file：删除自己的笔记产物（笔记不进检索知识库,删除无需任何入库/收敛动作）。
引用库的读写(查 key、入库、渲染参考文献)归 citation-agent——本角色不装配引用工具,
需要时派发它。阅读历史与未读清单的写入已收归 memory-agent（本角色只报告事件，不再自己记账）。
偏好有歧义时把问题写进最终回答（问用户不需要工具），不自行猜。
spawn 工具需要构造参数(agent_timeouts),故 make_tools 传已实例化的工具实例而非类。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, ExtractTitleTool, WriteFileTool, EditFileTool,
    DeleteFileTool, GlobTool, GrepTool,
)
from paperflow.tools.common.factory import make_tools
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools.vision.analyze_figures import AnalyzeFiguresTool


# 装配 10 工具:6 原子工具(含 delete_file 与 extract_title) + 共享 spawn_sub_agent
# + glob/grep + analyze_figures(笔记 §5 图表提取与视觉分析,embed_dir 落到笔记目录)。
# 审稿循环由 AGENT.md 驱动:spawn_sub_agent(agent_type=review-agent, task="审阅草稿文件
# <draft>,对照原文 <pdf>") 提交草稿,修订经 edit_file 覆盖写回同一最终路径,同时
# 兼顾"修改既有笔记"类任务。agent_timeouts 经 config 注入
# ——config 在 import 时构造(每进程静态、无副作用,对齐 make_tools 惯例)。
TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ExtractTitleTool,
    ReadFileTool, WriteFileTool, EditFileTool, DeleteFileTool,
    SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agents.timeouts),
    GlobTool, GrepTool,
    AnalyzeFiguresTool(),
], default_write_root="note")
