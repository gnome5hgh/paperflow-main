"""MemFS：记忆块的 git-backed markdown 投影层。

SQL blocks 是源，markdown 是投影——双向同步：块变更写文件 + git commit；
文件被人工编辑后检测并回写块。自动索引 memory_filesystem.md（自动生成，
不可编辑），供 LLM 感知「有哪些记忆文件存在」。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from paperflow.core.memory.schemas.block import Block

__all__ = ["MemFS"]

# 系统核心块标签：这类块会被存放在 system/ 子目录中
_SYSTEM_LABELS = {"assistant", "profile"}
# 索引文件名（自动生成，不应被人工编辑）
_INDEX_NAME = "memory_filesystem.md"


class MemFS:
    """记忆块与 markdown 文件之间的投影层：块 → 文件、文件 → 块、自动索引。

    核心职责：
        1. 正向同步：块变更时，将块内容写入对应的 .md 文件（含 frontmatter 元数据）。
        2. 反向同步：检测 .md 文件是否被人工修改，返回变更列表供调用方写回 SQL。
        3. 索引生成：自动维护 memory_filesystem.md 文件树，供 LLM 感知所有记忆文件。
    """

    def __init__(self, memory_dir: Path, db=None):
        """初始化 MemFS。

        Args:
            memory_dir: 记忆文件系统的根目录（所有 .md 文件将存放在此及子目录下）。
            db: 可选的 MemoryDB 实例，仅在 detect_file_changes() 需要读取 blocks 表时使用。
        """
        self.memory_dir = Path(memory_dir)
        # system 子目录存放 assistant/profile 等系统核心块
        self.system_dir = self.memory_dir / "system"
        self.db = db                       # MemoryDB | None（detect_file_changes 读 blocks 用）

    def _file_for(self, block: Block) -> Path:
        """返回块对应的投影文件路径：assistant/profile 进 system/ 子目录，其余放根目录。

        Args:
            block: 记忆块对象。

        Returns:
            该块对应的 .md 文件路径（不保证父目录存在）。

        设计原则：
            - 系统核心块（assistant/profile）单独放在 system/ 子目录，便于区分和管理。
            - 其他块（如 unread_list, history_list 等）直接放在 memory_dir 根目录，
              文件名为 {label}.md。
        """
        if block.label in _SYSTEM_LABELS:
            return self.system_dir / f"{block.label}.md"
        return self.memory_dir / f"{block.label}.md"

    def sync_block_to_file(self, block: Block) -> Path:
        """块 → markdown 投影（frontmatter 存 description/read_only/metadata）。

        Args:
            block: 要同步的块对象。

        Returns:
            写入的文件路径。

        生成的文件结构：
            ---
            description: 块描述
            read_only: true  # 仅当 block.read_only 为 True 时出现
            metadata: {key: value, ...}  # YAML 行内格式
            ---
            块的内容文本（block.value）

        注意：
            - 自动创建缺失的父目录。
            - 每次同步后自动调用 regenerate_index() 更新文件树索引。
            - metadata_ 使用 yaml.safe_dump 以行内格式（flow style）序列化，
              保持文件简洁可读。
        """
        # 确定目标路径并确保父目录存在
        path = self._file_for(block)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 构建 frontmatter 行
        meta_lines = [f"description: {block.description or ''}"]
        if block.read_only:
            meta_lines.append("read_only: true")
        # metadata_ 可能含嵌套结构，使用 YAML 行内格式序列化
        meta_lines.append("metadata: " + yaml.safe_dump(
            block.metadata_, default_flow_style=True, sort_keys=False).strip())

        # 组装 frontmatter 和正文
        front = "---\n" + "\n".join(meta_lines) + "\n---\n"
        # 写入文件（末尾加换行符便于阅读）
        path.write_text(front + block.value + "\n", encoding="utf-8")

        # 更新索引文件（包含该块条目）
        self.regenerate_index()
        return path

    def detect_file_changes(self) -> list[Block]:
        """扫描投影文件，值/描述与块不一致 → 返回需要回写的 Block 列表。

        这是「人改文件 → 回写 SQL」的反向通道：逐块比对块值与剥掉 frontmatter
        后的文件正文，不一致就把文件值写回 Block 对象，由调用方决定落库。

        Returns:
            发生变更的 Block 列表（仅设置 value 为文件内容，其余字段来自 DB）。

        注意：
            - 需要 self.db 不为 None，否则返回空列表（无法读取块表）。
            - 只检测 value 是否变化，暂不检测 description/metadata 的变化，
              如需完整双向同步可后续扩展。
            - 只处理已存在 .md 文件的块，缺失文件则跳过（可能是块已删除但文件残留）。
        """
        if self.db is None:
            return []
        from paperflow.core.memory.orm import block as block_orm

        changed: list[Block] = []
        # 遍历数据库中所有块
        for row in block_orm.select_blocks(self.db):
            block = _row_to_block(row)
            path = self._file_for(block)
            if not path.exists():
                continue  # 文件不存在，可能已被手动删除，跳过
            text = path.read_text(encoding="utf-8")
            # 剥离 frontmatter 获取正文
            value = _strip_frontmatter(text)
            # 若正文与块值不同，标记为变更
            if value != block.value:
                block.value = value
                changed.append(block)
        return changed

    def regenerate_index(self) -> None:
        """重新生成 memory_filesystem.md（文件树 + 各文件 description）。

        每次同步后重建；索引本身标「自动生成，请勿编辑」，且不把自己算进
        文件树（避免自引用）。

        生成内容格式：
            # Memory Filesystem（自动生成，请勿编辑）

            - `system/assistant.md` — 助手工作方式记忆
            - `unread_list.md` — 待读文献列表
            ...

        用途：供 LLM 通过读取该索引文件了解有哪些记忆块及其描述，便于按需读取。
        """
        lines = ["# Memory Filesystem（自动生成，请勿编辑）", ""]
        # 遍历所有 .md 文件（递归）
        for path in sorted(self.memory_dir.rglob("*.md")):
            # 跳过索引文件自身（避免自引用）
            if path.name == _INDEX_NAME:
                continue
            rel = path.relative_to(self.memory_dir)
            # 读取文件的 description frontmatter（若存在）
            desc = _frontmatter_field(path.read_text(encoding="utf-8"), "description")
            lines.append(f"- `{rel}`{(' — ' + desc) if desc else ''}")

        # 确保目录存在并写入索引
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / _INDEX_NAME).write_text("\n".join(lines) + "\n",
                                                   encoding="utf-8")


def _strip_frontmatter(text: str) -> str:
    """剥掉 markdown frontmatter（首行 --- 与结束 --- 之间），返回正文。

    Args:
        text: 含 frontmatter 的完整文件内容。

    Returns:
        剥离 frontmatter 后的正文（若没有 frontmatter 则原样返回）。

    实现细节：
        - 只识别以 "---" 开头且存在第二个 "---" 的情况。
        - 使用 split("---", 2) 分割为三部分：空字符串、frontmatter、正文。
        - 返回第三部分并去除首尾换行符（保留正文内格式）。
    """
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            # parts[2] 是正文（可能前面有换行符）
            return parts[2].strip("\n")
    return text


def _frontmatter_field(text: str, key: str) -> str:
    """读 frontmatter 中指定键的值（空字符串表示缺失/空值）。

    行内锚定：冒号后只允许水平空白，值不跨行——否则空 description 会吞到
    下一行 `---`，整个 frontmatter 解析被破坏。

    Args:
        text: 含 frontmatter 的完整文件内容。
        key: 要查找的键名（如 "description"）。

    Returns:
        键对应的值（字符串），若键不存在或值为空则返回空字符串。

    正则说明：
        - 匹配模式：^key:[ \t]*([^\r\n]*?)[ \t]*$ （多行模式）
        - 捕获组为冒号后的非空字符（去除首尾空白），但值不能包含换行。
        - 若值跨多行（如 YAML 块），此函数仅返回第一行，但当前设计所有字段都是单行。
    """
    m = re.search(rf"^{key}:[^\S\r\n]*([^\r\n]*?)[^\S\r\n]*$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _row_to_block(row: dict) -> Block:
    """把 DB 行转回 Block 模型（metadata_ / read_only 从 JSON/整型还原）。

    Args:
        row: 数据库行（dict 形式）。

    Returns:
        Block 实例。

    注意：此函数与 BlockManager._to_schema 逻辑一致，为保持 MemFS 独立而复制。
    """
    import json
    return Block(id=row["id"], label=row["label"], value=row["value"],
                 limit=row["limit"], description=row["description"],
                 metadata_=json.loads(row["metadata_"]) if row["metadata_"] else {},
                 read_only=bool(row["read_only"]), version=row["version"])