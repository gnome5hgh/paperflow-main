"""记忆工具集常量：记忆编辑工具名集合 + 首启播种的默认核心块文案。

BASE_MEMORY_TOOLS 是装配在 supervisor 上的记忆编辑工具名；BASE_SLEEPTIME_TOOLS
是 Sleeptime 后台整合允许生成的编辑工具子集（Sleeptime 只做块级增改，不做
unread_list/history_append 这类清单维护）。两者都只声明「工具名集合」，供
装配与校验读取。
"""

BASE_MEMORY_TOOLS = {
    "memory_replace", "memory_insert", "memory_rethink",
    "memory_finish_edits", "memory", "memory_apply_patch",
    "unread_list_add", "unread_list_remove", "history_append",
}
BASE_SLEEPTIME_TOOLS = {
    "memory_replace", "memory_insert", "memory_rethink", "memory_finish_edits",
}

#: 首启播种的核心记忆块默认文案（persona/human 缺失时由 ensure_default_blocks 创建）。
#: persona 是助手身份——与 agents/*/SKILL.md 的静态 system_prompt 分离，agent 可经
#: memory_replace 自我演进；human 是引导占位，提醒主 agent 对话中积累用户画像。
DEFAULT_PERSONA = (
    "你是 paperFlow，一个 LLM 驱动的学术研究工作流助手。"
    "你协助用户完成论文阅读、笔记、检索与研究流程。"
)
DEFAULT_HUMAN = (
    "用户画像（待维护）：由 supervisor 在对话中通过 "
    "memory_insert 逐步积累用户的身份、偏好、背景。"
)
