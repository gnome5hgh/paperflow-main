"""BlockManager：记忆块 CRUD + 乐观锁 + checkpoint_block 快照（撤销/重做）。

对应 Letta services/block_manager.py。GitEnabledBlockManager（git 变体）在
Task 4（memfs）中叠加。
"""
from __future__ import annotations

from pathlib import Path

from paperflow.core.memory.constants import DEFAULT_PERSONA, DEFAULT_HUMAN
from paperflow.core.memory.orm import block as block_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.block import Block

__all__ = ["BlockManager", "GitEnabledBlockManager"]

_READ_ONLY = "block is read-only"
_LIMIT = "Exceeds {limit} character limit"


class BlockManager:
    def __init__(self, db: MemoryDB):
        self.db = db

    def _to_schema(self, row: dict) -> Block:
        import json
        return Block(
            id=row["id"], label=row["label"], value=row["value"], limit=row["limit"],
            description=row["description"],
            metadata_=json.loads(row["metadata_"]) if row["metadata_"] else {},
            read_only=bool(row["read_only"]),
            version=row["version"],
        )

    def _db_history(self, label: str) -> list[dict]:
        """测试辅助：按 label 取该块的历史快照。"""
        row = block_orm.select_block_by_label(self.db, label)
        if row is None:
            return []
        return block_orm.select_block_history(self.db, row["id"])

    def create_block(self, label: str, value: str, limit: int = 2000,
                     description: str | None = None,
                     read_only: bool = False) -> Block:
        b = Block.new(label, value)
        b.limit = limit
        b.description = description
        b.read_only = read_only
        block_orm.insert_block(self.db, b, version=1)
        return b

    def get_block(self, block_id: str) -> Block:
        row = block_orm.select_block(self.db, block_id)
        if row is None:
            raise KeyError(f"block {block_id} not found")
        return self._to_schema(row)

    def get_block_by_label(self, label: str) -> Block | None:
        row = block_orm.select_block_by_label(self.db, label)
        return self._to_schema(row) if row else None

    def ensure_default_blocks(self) -> list[str]:
        """播种默认核心记忆块：persona/human 各自缺失才创建，绝不覆盖已有块。

        返回实际创建的 label 列表（无创建时为空）。persona 是助手身份、human 是
        用户画像引导占位——两者是 Memory.compile() 每轮渲染的 system/ 块，缺失时
        记忆系统呈空壳。幂等：已存在的块（含用户经 self-editing 改过的）不动。
        """
        created: list[str] = []
        for label, value in (("persona", DEFAULT_PERSONA),
                             ("human", DEFAULT_HUMAN)):
            if self.get_block_by_label(label) is None:
                self.create_block(label, value)
                created.append(label)
        return created

    def list_blocks(self) -> list[Block]:
        return [self._to_schema(r) for r in block_orm.select_blocks(self.db)]

    def update_block_value(self, label: str, value: str) -> Block:
        row = block_orm.select_block_by_label(self.db, label)
        if row is None:
            raise KeyError(f"block {label} not found")
        if row["read_only"]:
            raise ValueError(_READ_ONLY)
        if len(value) > row["limit"]:
            raise ValueError(_LIMIT.format(limit=row["limit"]))
        new_version = row["version"] + 1
        # checkpoint：改动前快照 → block_history（撤销/重做依据）
        block_orm.checkpoint_block(self.db, row["id"], row["label"], row["value"],
                                   row["limit"], row["description"],
                                   {}, row["version"])
        block_orm.update_block(self.db, row["id"], value, new_version)
        return self.get_block(row["id"])

    def delete_block(self, block_id: str) -> None:
        """删除块。read_only 块拒绝（与 update_block_value 一致），防误删保护块。"""
        row = block_orm.select_block(self.db, block_id)
        if row is None:
            raise KeyError(f"block {block_id} not found")
        if row["read_only"]:
            raise ValueError(_READ_ONLY)
        self._delete(block_id)

    def _delete(self, block_id: str) -> None:
        """底层删除钩子（rename 复用）：不检查 read_only，由 GitEnabled 子类扩展投影清理。"""
        block_orm.delete_block(self.db, block_id)

    def checkpoint_block(self, block_id: str) -> None:
        b = self.get_block(block_id)
        block_orm.checkpoint_block(self.db, b.id, b.label, b.value, b.limit,
                                   b.description, b.metadata_, 0)

    def restore_block(self, block_history_id: int) -> Block:
        snap = block_orm.restore_block_history(self.db, block_history_id)
        block_orm.update_block(self.db, snap["block_id"], snap["value"], snap["version"])
        return self.get_block(snap["block_id"])


class GitEnabledBlockManager(BlockManager):
    """BlockManager 的 git 变体：块变更同步到 MemFS markdown 投影并 git commit。

    对应 Letta GitEnabledBlockManager。原 paperFlow GitStore 由此保留——
    语义从「markdown 是源」变为「markdown 是 blocks 的投影」。
    """

    def __init__(self, db, memfs_dir: Path | None = None):
        super().__init__(db)
        from paperflow.core.memory.services.memfs import MemFS
        self.memfs = MemFS(memfs_dir or Path(db.path).parent, db=db)
        self._memfs_dir = self.memfs.memory_dir
        self._init_git()

    def _init_git(self) -> None:
        """惰性初始化 git 仓库（dulwich）。目录不存在时先创建（Repo.init 不建父目录）。"""
        from dulwich.repo import Repo
        self._memfs_dir.mkdir(parents=True, exist_ok=True)
        git_dir = self._memfs_dir / ".git"
        if not git_dir.exists():
            Repo.init(str(self._memfs_dir))
        self._repo = Repo(str(self._memfs_dir))

    def _git_log(self) -> list[str]:
        from dulwich.repo import Repo
        repo = Repo(str(self._memfs_dir))
        try:
            repo.head()
        except KeyError:
            return []
        # dulwich 1.x 的 get_walker() 返回 WalkEntry，commit 在 .commit 上
        return [c.commit.id.decode() for c in repo.get_walker(max_entries=50)]

    def _commit(self, message: str) -> str | None:
        """只跟踪 *.md，无变更返回 None（不产生空 commit）。"""
        import dulwich.porcelain as porcelain
        from dulwich.repo import Repo
        repo = Repo(str(self._memfs_dir))
        changed = False
        for path in sorted(self._memfs_dir.rglob("*.md")):
            rel = str(path.relative_to(self._memfs_dir))
            porcelain.add(repo, rel)
            changed = True
        if not changed:
            return None
        status = porcelain.status(repo)
        staged = status.staged.get("add", []) + status.staged.get("modify", [])
        if not staged:
            return None
        author = b"paperFlow <paperflow@local>"
        sha = porcelain.commit(repo, message=message, author=author, committer=author)
        return sha.decode() if isinstance(sha, bytes) else str(sha)

    def create_block(self, label: str, value: str, **kwargs) -> Block:
        b = super().create_block(label, value, **kwargs)
        self.memfs.sync_block_to_file(b)
        self._commit(f"create block {label}")
        return b

    def update_block_value(self, label: str, value: str) -> Block:
        b = super().update_block_value(label, value)
        self.memfs.sync_block_to_file(b)
        self._commit(f"update block {label}")
        return b

    def delete_block(self, block_id: str) -> None:
        # 基类 delete_block 校验 read_only 后走 self._delete()（投影清理 + git commit）
        super().delete_block(block_id)

    def _delete(self, block_id: str) -> None:
        """删 SQL + 同步删 MemFS 投影 .md + git commit + 重建索引（不留孤儿投影/陈旧索引）。"""
        b = self.get_block(block_id)
        super()._delete(block_id)
        path = self.memfs._file_for(b)
        if path.exists():
            path.unlink()
        self.memfs.regenerate_index()
        self._commit(f"delete block {b.label}")
