"""记忆目录的 Git 版本跟踪。

GitStore 在记忆目录内维护一个独立的 Git 仓库，只跟踪 *.md 文件，每次写入后
自动提交，并提供查看提交历史与恢复文件到指定提交的能力。
"""
import os
from pathlib import Path

import dulwich.porcelain
from dulwich.repo import Repo


class GitStore:
    """在记忆目录内维护独立 Git 仓库，只跟踪 *.md 文件，每次写入后自动提交。"""

    AUTHOR = ("paperFlow", "paperflow@local")    # dulwich 提交时必填的作者信息

    def __init__(self, memory_dir: Path):
        """保存记忆目录路径；仓库对象采用惰性加载。"""
        self.repo_dir = Path(memory_dir)
        self._repo: Repo | None = None

    def _ensure_repo(self) -> Repo:
        """确保仓库已初始化并返回 dulwich Repo 对象（首次访问时惰性创建）。"""
        if self._repo is None:
            git_dir = self.repo_dir / ".git"
            if not git_dir.exists():
                Repo.init(str(self.repo_dir))
            self._repo = Repo(str(self.repo_dir))
        return self._repo

    def commit(self, message: str) -> str | None:
        """自动提交：只跟踪 *.md 文件；没有实际变更时返回 None，不产生空提交。"""
        repo = self._ensure_repo()
        changed = False
        for path in sorted(self.repo_dir.glob("*.md")):
            rel = os.path.relpath(path, self.repo_dir)
            dulwich.porcelain.add(repo, rel)
            changed = True
        if not changed:
            return None
        status = dulwich.porcelain.status(repo)
        staged = status.staged.get("add", []) + status.staged.get("modify", [])
        if not staged:
            return None
        author_name, author_email = self.AUTHOR
        author = f"{author_name} <{author_email}>".encode()
        sha = dulwich.porcelain.commit(
            repo, message=message,
            author=author,
            committer=author,
        )
        return sha.decode() if isinstance(sha, bytes) else sha

    def log(self, max_entries: int = 20) -> list[dict]:
        """返回最近的提交记录列表，每条包含 sha 与提交信息；空仓库返回空列表。"""
        repo = self._ensure_repo()
        try:
            repo.head()
        except KeyError:
            return []    # 空仓库（尚无提交，HEAD 未指向任何对象）：无日志可查
        entries = []
        for entry in repo.get_walker(max_entries=max_entries):
            commit = entry.commit
            entries.append({
                "sha": commit.id.decode(),
                "message": commit.message.decode(errors="replace").strip(),
            })
        return entries

    def revert(self, commit_sha: str) -> None:
        """把记忆文件恢复到指定提交时的内容，并让 HEAD 指向该提交。"""
        repo = self._ensure_repo()
        # dulwich 1.2.12 没有提供 checkout_tree / reset_hard，用 reset hard 达到等价效果
        dulwich.porcelain.reset(repo, "hard", commit_sha.encode())
