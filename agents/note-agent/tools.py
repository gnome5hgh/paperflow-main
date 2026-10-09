"""note-agent 的工具装配:原子文件工具 + 派发 review-agent 的审稿 spawn。

装配 read_file/read_pdf/write_file/edit_file/delete_file 五个原子工具(复用
paperflow/tools/ 的集中式安全边界与风险语义)、glob/grep 定位工具、ask_user_question
交互工具(格式/篇幅/语言偏好歧义时中途问用户),以及 SpawnSubAgentTool——AGENT.md
的审稿循环用它派发 review-agent 子 agent 审阅草稿,拿回裁决后经 edit_file 修订。
delete_file：删除自己的笔记产物——删除成功后派发 rag-agent 全量收敛,清掉它的索引块。
引用库的读写(查 key、入库、渲染参考文献)归 citation-agent——本角色不装配引用工具,
需要时派发它。阅读历史与未读清单的写入已收归 memory-agent（本角色只报告事件，不再自己记账）。
spawn 工具需要构造参数(agent_timeouts),故 make_tools 传已实例化的工具实例而非类。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, WriteFileTool, EditFileTool, DeleteFileTool,
    GlobTool, GrepTool, AskUserQuestionTool,
)
from paperflow.tools.common.factory import make_tools
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools.vision.analyze_figures import AnalyzeFiguresTool


# 装配 10 工具:5 原子工具(含 delete_file) + ask_user_question + 共享 spawn_sub_agent
# + glob/grep + analyze_figures(笔记 §5 图表提取与视觉分析,embed_dir 落到笔记目录)。
# 审稿循环由 AGENT.md 驱动:spawn_sub_agent(agent_type=review-agent, task="审阅草稿文件
# <draft>,对照原文 <pdf>") 提交草稿,修订经 edit_file 覆盖写回同一最终路径,同时
# 兼顾"修改既有笔记"类任务。agent_timeouts 经 config 注入
# ——config 在 import 时构造(每进程静态、无副作用,对齐 make_tools 惯例)。
TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ReadFileTool, WriteFileTool, EditFileTool, DeleteFileTool,
    SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agents.timeouts),
    GlobTool, GrepTool, AskUserQuestionTool,
    AnalyzeFiguresTool(),
], default_write_root="note")
