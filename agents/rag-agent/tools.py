"""rag-agent 的工具装配：只有索引写入的两个工具。

不给读文件工具（路径存在性与跳过原因由入库工具自己报）、不给 glob/grep（它不需要
自己找文件，路径由调用方给出）、不给 ask_user_question（失败如实回报，是否请示用户
由上级决定）。也不装记忆工具：索引维护不产生需要沉淀的清单或历史。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools.rag import IndexPathsTool, ReindexAllTool

# config 在 import 时构造（每进程静态、无副作用，对齐 make_tools 惯例）
TOOLS = make_tools(PaperFlowConfig.from_env(), [
    IndexPathsTool, ReindexAllTool,
])
