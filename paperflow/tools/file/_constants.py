"""工具共享常量（私有模块，下划线前缀——不进 __init__ 再导出）。"""

#: 可写的目录语义根（Note 可写；Paper=pdf 只读，SCOPE 硬边界）
#: 拆分前为 file.py 的 `_NOTE_ROOTS`，Write/Edit 两个工具共享——单一事实来源防漂移
NOTE_ROOTS = ["note", "memory"]
