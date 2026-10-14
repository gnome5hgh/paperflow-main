"""工具声明的副作用类型。"""

from enum import StrEnum

__all__ = ["SideEffect"]


class SideEffect(StrEnum):
    """工具声明的副作用类型（可多选）。"""

    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"
    NETWORK = "network"
    READ_FILE = "read_file"
