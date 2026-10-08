"""记忆模块的常量——工具名集合与首启播种的核心块文案。

BASE_MEMORY_TOOLS 是装配在 supervisor 上的记忆编辑工具名；BASE_SLEEPTIME_TOOLS
是 Sleeptime 后台整合允许生成的编辑工具子集（Sleeptime 只做块级增改，不做
unread_list/history_append 这类清单维护）。两者都只声明「工具名集合」，供
装配与校验读取。
"""

__all__ = ["BASE_MEMORY_TOOLS", "BASE_SLEEPTIME_TOOLS", "DEFAULT_PROFILE", "DEFAULT_ASSISTANT"]

BASE_MEMORY_TOOLS = {
    "memory_replace", "memory_insert", "memory_rethink",
    "memory_finish_edits", "memory", "memory_apply_patch",
    "unread_list_add", "unread_list_remove", "history_append",
}
BASE_SLEEPTIME_TOOLS = {
    "memory_replace", "memory_insert", "memory_rethink", "memory_finish_edits",
}

#: 首启播种的核心记忆块默认文案（profile/assistant 缺失时由 ensure_default_blocks 创建）。
#: assistant 是助手工作方式记忆——与 agents/*/AGENT.md 的静态 system_prompt 分离，
#: 可经 memory_replace 自我演进；profile 是用户画像引导占位，提醒主 agent 对话中积累用户画像。
DEFAULT_PROFILE = (
    "用户画像（待维护）：由 supervisor 在对话中通过 "
    "memory_insert 逐步积累用户的身份、偏好、背景。"
)
DEFAULT_ASSISTANT = (
    "工作方式记忆（由 sleeptime 维护）：记录与用户协作中学到的"
    "助手角色调整与工作方式偏好；当前为空。"
)
