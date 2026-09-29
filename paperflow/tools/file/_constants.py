"""工具共享常量(私有模块,下划线前缀——不进 __init__ 再导出)。"""

#: write/edit 工具共用的 root_hints（单一事实来源,防两个工具各自维护导致漂移）。
#: 仅作 [目录] 提示;强制边界在 WorkspacePolicyMiddleware。
NOTE_HINTS = ["note", "memory", "research"]
