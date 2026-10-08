"""BlockManager：记忆块 CRUD + 写前快照 + 单调版本号（撤销/重做的历史链）。

更新块前把旧值整体快照进 block_history、版本号 +1；写入走 CAS（带期望版本的
条件更新），版本被并发推进时拒绝而非静默覆盖。读-改-写类操作（追加/替换/删除
行）必须走 mutate_block——它把整段序列放进一次持锁事务，避免两个并发写者各自
基于同一份旧值计算后互相抹掉。GitEnabledBlockManager 是其 git 变体：块变更同步
写 markdown 投影 + git commit，语义是「SQL 是源、markdown 是投影」。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from paperflow.core.memory.constants import DEFAULT_ASSISTANT, DEFAULT_PROFILE
from paperflow.core.memory.errors import ConcurrentUpdateError
from paperflow.core.memory.orm import block as block_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.block import Block

__all__ = ["BlockManager", "GitEnabledBlockManager"]

logger = logging.getLogger(__name__)

_READ_ONLY = "block is read-only"
_LIMIT = "Exceeds {limit} character limit"

#: CAS 冲突后的重试上限：重试意味着重读最新值并重放 mutator（要求 mutator 是纯函数）。
_CAS_MAX_RETRIES = 3

#: 旧核心块 label → 新 label（一次性迁移映射：human/persona 收敛为 profile/assistant）
_LEGACY_LABELS = {"human": "profile", "persona": "assistant"}


class BlockManager:
    """核心块业务层：持有 read_only / limit 不变式，维护写前快照历史链。

    Attributes:
        db: MemoryDB，blocks/block_history 表的连接
    """

    def __init__(self, db: MemoryDB):
        """初始化块管理器。

        Args:
            db: 数据库连接（需已初始化并持有全局写锁）。
        """
        self.db = db

    def _to_schema(self, row: dict) -> Block:
        """把 DB 行转回 Block 模型（metadata_ / read_only 从 JSON/整型还原）。

        Args:
            row: 数据库行（dict 形式，含 id, label, value, limit, description,
                metadata_, read_only, version）。

        Returns:
            填充好的 Block 实例。

        注意：metadata_ 在 DB 中为 JSON 字符串，需反序列化为 dict。
        """
        import json
        return Block(
            id=row["id"], label=row["label"], value=row["value"], limit=row["limit"],
            description=row["description"],
            metadata_=json.loads(row["metadata_"]) if row["metadata_"] else {},
            read_only=bool(row["read_only"]),
            version=row["version"],
        )

    def _db_history(self, label: str) -> list[dict]:
        """测试辅助：按 label 取该块的历史快照。

        Args:
            label: 块的标签名称。

        Returns:
            该块的历史快照列表（block_history 表行），若块不存在返回空列表。
        """
        row = block_orm.select_block_by_label(self.db, label)
        if row is None:
            return []
        return self.get_block_history(row["id"])

    def create_block(self, label: str, value: str, limit: int = 2000,
                     description: str | None = None,
                     read_only: bool = False) -> Block:
        """新建块并落盘（初始版本号 1）。

        Args:
            label: 块的标签（唯一标识符，如 "assistant", "profile"）。
            value: 块的内容文本。
            limit: 字符上限（默认 2000），更新时若超过将拒绝。
            description: 块的描述（可选，用于展示）。
            read_only: 是否为只读块（只读块不可更新/删除，默认 False）。

        Returns:
            新创建的 Block 实例（含自动生成的 id 和初始 version=1）。
        """
        b = Block.new(label, value)
        b.limit = limit
        b.description = description
        b.read_only = read_only
        block_orm.insert_block(self.db, b, version=1)
        return b

    def get_block(self, block_id: str) -> Block:
        """按 id 取块；不存在抛 KeyError（变异工具硬失败的来源）。

        Args:
            block_id: 块的唯一标识。

        Returns:
            块对象。

        Raises:
            KeyError: 当 block_id 不存在时。
        """
        row = block_orm.select_block(self.db, block_id)
        if row is None:
            raise KeyError(f"block {block_id} not found")
        return self._to_schema(row)

    def get_block_by_label(self, label: str) -> Block | None:
        """按 label 取块；不存在返回 None（工具层据此判断「该建还是该报错」）。

        Args:
            label: 块的标签名称。

        Returns:
            块对象或 None。
        """
        row = block_orm.select_block_by_label(self.db, label)
        return self._to_schema(row) if row else None

    def get_block_history(self, block_id: str) -> list[dict]:
        """按块 id 返回其全部历史快照（从旧到新，即 block_history 表行）。

        Args:
            block_id: 块的唯一标识。

        Returns:
            该块的历史快照列表（含 version/value 等列）；无历史时为空列表。
        """
        return block_orm.select_block_history(self.db, block_id)

    def migrate_legacy_labels(self) -> list[str]:
        """把旧核心块 label（human/persona）迁移为 profile/assistant（幂等）。

        在 ensure_default_blocks 之前调用：先迁移再播种，保证旧库不会因
        「新 label 缺失」而被播种出第二套并存块。value/id/创建时间原样保留。

        Returns:
            实际迁移到的新 label 列表（无迁移时为空）。
        """
        migrated: list[str] = []
        for old, new in _LEGACY_LABELS.items():
            old_block = self.get_block_by_label(old)
            if old_block is None:
                continue
            if self.get_block_by_label(new) is not None:
                logger.warning("label 迁移跳过：%s 与 %s 并存，请人工处理", old, new)
                continue
            block_orm.update_block_label(self.db, old_block.id, new)
            migrated.append(new)
        return migrated

    def ensure_default_blocks(self) -> list[str]:
        """播种默认核心记忆块：assistant/profile 各自缺失才创建，绝不覆盖已有块。

        Returns:
            实际创建的 label 列表（无创建时为空）。

        设计意图：
            - assistant 是助手自我认知/工作方式记忆、profile 是用户画像引导占位
              ——两者是 Memory.compile() 每轮渲染的 system/ 块，缺失时记忆系统呈空壳。
            - 幂等性：已存在的块（含用户经 self-editing 改过的）不动，避免意外覆盖。
        """
        created: list[str] = []
        for label, value in (("assistant", DEFAULT_ASSISTANT),
                             ("profile", DEFAULT_PROFILE)):
            if self.get_block_by_label(label) is None:
                self.create_block(label, value)
                created.append(label)
        return created

    def list_blocks(self) -> list[Block]:
        """返回全部块（按创建时间排序），供 Memory 重建与 head 编译。

        Returns:
            所有块的列表（按 DB 插入顺序，即创建时间升序）。
        """
        return [self._to_schema(r) for r in block_orm.select_blocks(self.db)]

    def update_block_value(self, label: str, value: str) -> Block:
        """把块值整体设为 value（原子：读取、快照、写入在一次持锁内完成）。

        Args:
            label: 块的标签。
            value: 新的内容文本。

        Returns:
            更新后的 Block 对象（含新的 version）。

        Raises:
            KeyError: label 不存在。
            ValueError: 块为 read_only 或新 value 超限。
            ConcurrentUpdateError: 读取后写入前版本被其他写者推进（CAS 拒绝）。
        """
        with self.db.transaction():
            # 读取当前块并校验不变式
            row = block_orm.select_block_by_label(self.db, label)
            if row is None:
                raise KeyError(f"block {label} not found")
            if row["read_only"]:
                raise ValueError(_READ_ONLY)
            if len(value) > row["limit"]:
                raise ValueError(_LIMIT.format(limit=row["limit"]))
            # checkpoint：改动前快照 → block_history（撤销/重做依据）
            block_orm.checkpoint_block(self.db, row["id"], row["label"], row["value"],
                                       row["limit"], row["description"],
                                       {}, row["version"])
            # CAS 写入：期望版本仍为读到的版本，否则抛冲突
            block_orm.update_block(self.db, row["id"], value,
                                   expected_version=row["version"])
        block = self.get_block(row["id"])
        self._after_block_write(block)
        return block

    def mutate_block(self, label: str,
                     mutate: Callable[[str], str | None]) -> Block | None:
        """原子读-改-写：读当前值 → 交给 mutate 计算新值 → 写回，整段一次持锁。

        追加/替换类操作必须走这里：它们的「读旧值 → 算新值 → 写」跨多次调用，分开
        执行时两个并发写者会各自基于同一份旧值计算，后写者把前者的改动抹掉。

        Args:
            label: 目标块标签。
            mutate: 接收当前块值、返回新值的纯函数。返回 None 表示「判定不改」——
                不写、不推进版本，直接返回 None（调用方据此回报未命中，如替换锚点
                不存在）。必须是旧值的纯函数（只依赖入参），CAS 冲突后会在最新值上
                重放它；依赖外部读到的状态会让重放失真。

        Returns:
            更新后的 Block；mutate 返回 None 时为 None。

        Raises:
            KeyError: label 不存在。
            ValueError: 块为 read_only 或新值超限（含 mutator 抛出的校验错误）。
            ConcurrentUpdateError: 连续重试仍冲突（说明有绕过本入口的写者）。
        """
        for _ in range(_CAS_MAX_RETRIES):
            try:
                with self.db.transaction():
                    row = block_orm.select_block_by_label(self.db, label)
                    if row is None:
                        raise KeyError(f"block {label} not found")
                    if row["read_only"]:
                        raise ValueError(_READ_ONLY)
                    new_value = mutate(row["value"])
                    if new_value is None:
                        return None
                    if len(new_value) > row["limit"]:
                        raise ValueError(_LIMIT.format(limit=row["limit"]))
                    block_orm.checkpoint_block(
                        self.db, row["id"], row["label"], row["value"],
                        row["limit"], row["description"], {}, row["version"])
                    block_orm.update_block(self.db, row["id"], new_value,
                                           expected_version=row["version"])
                block = self.get_block(row["id"])
                self._after_block_write(block)
                return block
            except ConcurrentUpdateError:
                continue          # 重读最新值后重放 mutate（纯函数，重放无损）
        raise ConcurrentUpdateError(label)

    def _after_block_write(self, block: Block) -> None:
        """块内容写成功后的扩展钩子（基类无副作用）。

        Args:
            block: 刚写入的块。

        设计意图：把「写成功之后要做的同步」集中到一个钩子，update_block_value 与
        mutate_block 都调用它；子类（如 GitEnabled）只需实现一次就能覆盖所有写入口，
        不必逐个覆盖写方法。
        """

    def delete_block(self, block_id: str) -> None:
        """删除块。read_only 块拒绝（与 update_block_value 一致），防误删保护块。

        Args:
            block_id: 块的唯一标识。

        Raises:
            KeyError: block_id 不存在。
            ValueError: 块为 read_only。

        注意：实际删除委托给 _delete() 钩子，便于子类扩展（如 GitEnabled 清理投影）。
        """
        row = block_orm.select_block(self.db, block_id)
        if row is None:
            raise KeyError(f"block {block_id} not found")
        if row["read_only"]:
            raise ValueError(_READ_ONLY)
        self._delete(block_id)

    def _delete(self, block_id: str) -> None:
        """底层删除钩子（rename 复用）：不检查 read_only，由 GitEnabled 子类扩展投影清理。

        Args:
            block_id: 块的唯一标识。

        设计意图：将实际删除逻辑抽离，使子类可重写此方法添加清理动作（如删除对应 .md 文件），
        同时基类的 delete_block 保持只读校验不变。
        """
        block_orm.delete_block(self.db, block_id)

    def checkpoint_block(self, block_id: str) -> None:
        """手动快照：把当前块状态写进 block_history（version 记为 0，表示非更新前快照）。

        Args:
            block_id: 块的唯一标识。

        用途：允许在未发生更新的情况下手动保存一个检查点，例如在重大操作前备份。
        version=0 用于区分「手动检查点」和「更新前自动快照」（更新前快照的 version 为旧版本号）。
        """
        b = self.get_block(block_id)
        block_orm.checkpoint_block(self.db, b.id, b.label, b.value, b.limit,
                                   b.description, b.metadata_, 0)

    def restore_block(self, block_history_id: int) -> Block:
        """按历史快照回滚块：把快照的值写回 blocks 行，返回回滚后的块。

        Args:
            block_history_id: block_history 表的自增主键 ID。

        Returns:
            回滚后的 Block 对象。

        Raises:
            KeyError: 快照不存在，或其对应块行已被删除。

        回滚逻辑：
            - 从 block_history 读取快照数据（包含 block_id, value）。
            - 以「当前行版本」为 CAS 期望版本，把快照值写回 blocks 行：写入成功后
              版本推进为当前版本 + 1（回滚也照常推进版本，不做版本回退）。快照里的
              version 是历史写入时的标注，不能当 CAS 期望值。
            - 注意：回滚不会生成新的历史记录（不记录“回滚操作”本身），
              如需可撤销的回滚，调用方可在回滚后手动调用 checkpoint_block。
        """
        snap = block_orm.restore_block_history(self.db, block_history_id)
        cur = block_orm.select_block(self.db, snap["block_id"])
        if cur is None:
            # 块行已删、只剩历史快照：报「块不存在」，与 get_block 的失败语义一致。
            raise KeyError(f"block {snap['block_id']} not found")
        block_orm.update_block(self.db, snap["block_id"], snap["value"],
                               expected_version=cur["version"])
        return self.get_block(snap["block_id"])


class GitEnabledBlockManager(BlockManager):
    """BlockManager 的 git 变体：块变更同步到 MemFS markdown 投影并 git commit。

    语义是「SQL 是源、markdown 是投影」：权威数据在 blocks 表，.md 文件只是
    给人看/给人手改的可读投影，git 只做可审计历史（每写必 commit，无变更不
    产生空 commit）。

    Attributes:
        memfs: MemFS，markdown 投影层，每次块变更同步并 commit
        _memfs_dir: Path，MemFS 根目录（同时是 git 仓库工作区）
        _repo: dulwich Repo，投影目录的 git 仓库句柄
    """

    def __init__(self, db, memfs_dir: Path | None = None):
        """初始化 Git 增强块管理器。

        Args:
            db: 数据库连接。
            memfs_dir: 记忆文件系统根目录（默认取 db.path 的父目录）。
        """
        super().__init__(db)
        from paperflow.core.memory.services.memfs import MemFS
        self.memfs = MemFS(memfs_dir or Path(db.path).parent, db=db)
        self._memfs_dir = self.memfs.memory_dir
        self._init_git()

    def _init_git(self) -> None:
        """惰性初始化 git 仓库（dulwich）。目录不存在时先创建（Repo.init 不建父目录）。"""
        from dulwich.repo import Repo
        # 确保目录存在（Repo.init 不创建父目录）
        self._memfs_dir.mkdir(parents=True, exist_ok=True)
        git_dir = self._memfs_dir / ".git"
        if not git_dir.exists():
            Repo.init(str(self._memfs_dir))
        self._repo = Repo(str(self._memfs_dir))

    def _git_log(self) -> list[str]:
        """返回最近最多 50 条 commit 的 sha（无提交历史时返回空列表）。

        Returns:
            commit SHA 字符串列表，按时间倒序（最新优先）。

        注意：dulwich 1.x 的 get_walker() 返回 WalkEntry，commit 在 .commit 属性上。
        """
        from dulwich.repo import Repo
        repo = Repo(str(self._memfs_dir))
        try:
            repo.head()
        except KeyError:
            # 无任何 commit 时返回空列表
            return []
        # dulwich 1.x 的 get_walker() 返回 WalkEntry，commit 在 .commit 上
        return [c.commit.id.decode() for c in repo.get_walker(max_entries=50)]

    def _commit(self, message: str) -> str | None:
        """只跟踪 *.md，无变更返回 None（不产生空 commit）。

        Args:
            message: commit 信息。

        Returns:
            commit SHA（字符串），若无变更则返回 None。

        实现细节：
            1. 用 dulwich.porcelain.add 添加所有 *.md 文件（无论是否变更）。
            2. 用 porcelain.status 检查 staged 区是否有新增或修改。
            3. 若无 staged 变更，返回 None 避免空 commit。
            4. 使用固定作者信息 "paperFlow <paperflow@local>"。
        """
        import dulwich.porcelain as porcelain
        from dulwich.repo import Repo
        repo = Repo(str(self._memfs_dir))

        # 1. 添加所有 *.md 文件到暂存区（add 对未变更文件也是幂等的）
        changed = False
        for path in sorted(self._memfs_dir.rglob("*.md")):
            rel = str(path.relative_to(self._memfs_dir))
            porcelain.add(repo, rel)
            changed = True

        # 2. 若没有找到任何 .md 文件，直接返回（无需 commit）
        if not changed:
            return None

        # 3. 检查 staged 区是否有实际变更（新增或修改）
        status = porcelain.status(repo)
        staged = status.staged.get("add", []) + status.staged.get("modify", [])
        if not staged:
            return None

        # 4. 执行 commit
        author = b"paperFlow <paperflow@local>"
        sha = porcelain.commit(repo, message=message, author=author, committer=author)
        return sha.decode() if isinstance(sha, bytes) else str(sha)

    def create_block(self, label: str, value: str, **kwargs) -> Block:
        """创建块并同步到 markdown 投影 + git commit。

        Args:
            label: str，块标签
            value: str，块内容
            kwargs: 透传给基类 create_block 的可选字段（description/read_only 等）

        Returns:
            创建后的 Block（已写入 markdown 投影并 commit）。
        """
        b = super().create_block(label, value, **kwargs)
        self.memfs.sync_block_to_file(b)
        self._commit(f"create block {label}")
        return b

    def _after_block_write(self, block: Block) -> None:
        """写完块后同步 MemFS markdown 投影 + git commit（每写必 commit，无变更不空提交）。

        Args:
            block: 刚写入的块。

        覆盖基类钩子而非分别覆盖各写方法：这样 update_block_value 与 mutate_block
        两个入口都自动带上投影同步，不会出现「只落 SQL、漏投影」的分叉。
        """
        self.memfs.sync_block_to_file(block)
        self._commit(f"update block {block.label}")

    def delete_block(self, block_id: str) -> None:
        """删除块：先执行基类校验（read_only 检查），然后走 _delete 清理投影。

        Args:
            block_id: str，要删除的块 ID
        """
        # 基类 delete_block 校验 read_only 后走 self._delete()（投影清理 + git commit）
        super().delete_block(block_id)

    def _delete(self, block_id: str) -> None:
        """删 SQL + 同步删 MemFS 投影 .md + git commit + 重建索引（不留孤儿投影/陈旧索引）。

        Args:
            block_id: 要删除的块 ID。

        清理顺序：
            1. 获取块对象（用于定位 .md 文件路径）。
            2. 调用父类 _delete 删除 SQL 记录。
            3. 删除对应的 .md 投影文件（若存在）。
            4. 重建 memory_filesystem.md 索引（去掉该块条目）。
            5. 提交 git commit。
        """
        # 先获取块信息（以便后续删除文件），再删除 SQL 记录
        b = self.get_block(block_id)
        super()._delete(block_id)

        # 删除对应的 markdown 投影文件
        path = self.memfs._file_for(b)
        if path.exists():
            path.unlink()

        # 重建文件树索引（确保索引中不再包含被删除的块）
        self.memfs.regenerate_index()

        # 提交 git
        self._commit(f"delete block {b.label}")