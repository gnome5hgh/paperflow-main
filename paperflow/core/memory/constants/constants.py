"""记忆模块的常量——记忆工具词汇与首启播种的核心块文案。

BASE_MEMORY_TOOLS 是 LLM 面记忆工具的词表（blocks 6 + 未读清单 2；对话检索
`conversation_search` 由工具包单独补上）。真正的装配面由 `get_memory_tools()` 决定，
这里是「这一套工具叫什么」的单一声明点，供测试与文档对齐。整合器的编辑面是结构化
指令（动作 + 目标类型），不经过工具名，故不在此列。
"""

__all__ = ["BASE_MEMORY_TOOLS", "DEFAULT_PROFILE", "DEFAULT_ASSISTANT"]

BASE_MEMORY_TOOLS = {
    "memory_replace", "memory_insert", "memory_rethink",
    "memory_finish_edits", "memory", "memory_apply_patch",
    "unread_list_add", "unread_list_remove",
}

#: 首启播种的核心记忆块默认文案（profile/assistant 缺失时由 ensure_default_blocks 创建）。
#: assistant 是助手工作方式记忆——与 agents/*/AGENT.md 的静态 system_prompt 分离，
#: 可经 memory_replace 自我演进；profile 是用户画像引导占位，提醒主 agent 对话中积累用户画像。
DEFAULT_PROFILE = (
    "用户画像（待维护）：由 supervisor 在对话中通过 "
    "memory_insert 逐步积累用户的身份、偏好、背景。"
)
DEFAULT_ASSISTANT = (
    "工作方式记忆（由 consolidation 维护）：记录与用户协作中学到的"
    "助手角色调整与工作方式偏好；当前为空。"
)
