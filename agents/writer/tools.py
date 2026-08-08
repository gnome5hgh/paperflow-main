# agents/writer/tools.py（7 工具完整装配：5 原子工具 + 共享 spawn + glob/grep）
"""writer 的工具装配。

5 个原子工具（read_file/read_pdf/write_file/edit_file/mark_read）复用
paperflow/tools/ 的集中式安全边界（WorkspacePolicy 白名单、风险语义）。
审稿不再用 agent 目录内的 ReviewDraftTool 桥（Task 7 删除），改直接装配共享
SpawnSubAgentTool（paperflow/tools/spawn.py，与 supervisor 同款派发）——
reviewer 子 agent 由 spawn 工具运行时构造并校验 allowed_spawns。spawn 工具需要
构造参数（agent_timeouts），故 make_tools 传"已实例化的工具"而非类（factory 支持二者）。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, WriteFileTool, EditFileTool, MarkReadTool,
    GlobTool, GrepTool,
)
from paperflow.tools.factory import make_tools
from paperflow.tools.spawn import SpawnSubAgentTool


# 完整 7 工具：5 原子工具 + 共享 spawn_sub_agent（Task 7 替代 review_draft 桥）+
# glob/grep（Task 4 定位）。SKILL.md 的审稿循环用 spawn_sub_agent(agent_type=reviewer,
# task="审阅草稿文件 <draft>，对照原文 <pdf>") 提交草稿，edit_file 进循环做修订
# （A-ii：覆盖写回同一最终路径），同时留给"修改既有笔记"类任务。
# agent_timeouts 经 config 注入（D2：按 agent 解析子 agent 超时）；config 在 import
# 时构造（每进程静态、无副作用，对齐 make_tools 惯例）。
TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ReadFileTool, WriteFileTool, EditFileTool, MarkReadTool,
    SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agent_timeouts),
    GlobTool, GrepTool,
])
