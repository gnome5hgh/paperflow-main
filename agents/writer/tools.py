"""writer 的工具装配：原子文件工具 + 派发 reviewer 的审稿 spawn。

装配 read_file/read_pdf/write_file/edit_file 四个原子工具(复用
paperflow/tools/ 的集中式安全边界与风险语义)、glob/grep 定位工具、ask_user_question
交互工具(格式/篇幅/语言偏好歧义时中途问用户),以及 SpawnSubAgentTool——SKILL
的审稿循环用它派发 reviewer 子 agent 审阅草稿,拿回裁决后经 edit_file 修订。
spawn 工具需要构造参数(agent_timeouts),故 make_tools 传已实例化的工具实例而非类。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, WriteFileTool, EditFileTool,
    GlobTool, GrepTool, AskUserQuestionTool,
)
from paperflow.tools.common.factory import make_tools
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool


# 完整装配 8 工具:4 原子工具 + ask_user_question + 共享 spawn_sub_agent + glob/grep。
# 审稿循环由 SKILL 驱动:spawn_sub_agent(agent_type=reviewer, task="审阅草稿文件
# <draft>,对照原文 <pdf>") 提交草稿,修订经 edit_file 覆盖写回同一最终路径,同时
# 兼顾"修改既有笔记"类任务。agent_timeouts 经 config 注入——config 在 import 时构造
# (每进程静态、无副作用,对齐 make_tools 惯例)。
TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ReadFileTool, WriteFileTool, EditFileTool,
    SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agent_timeouts),
    GlobTool, GrepTool, AskUserQuestionTool,
])
