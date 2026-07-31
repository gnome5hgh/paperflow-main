import os
from pathlib import Path

import dulwich.porcelain
from dulwich.repo import Repo


class GitStore:
    """data/memory/ 下的独立 .git 仓库，跟踪 *.md 文件，每次写入后 auto-commit。"""

    AUTHOR = ("paperFlow", "paperflow@local")    # dulwich do_commit 必需

    def __init__(self, memory_dir: Path):
        self.repo_dir = Path(memory_dir)
        self._repo: Repo | None = None

    def _ensure_repo(self) -> Repo:
        if self._repo is None:
            git_dir = self.repo_dir / ".git"
            if not git_dir.exists():
                Repo.init(str(self.repo_dir))
            self._repo = Repo(str(self.repo_dir))
        return self._repo

    def commit(self, message: str) -> str | None:
        """auto-commit：只跟踪 *.md。无变更返回 None（不产生空 commit）。"""
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
        repo = self._ensure_repo()
        entries = []
        for entry in repo.get_walker(max_entries=max_entries):
            commit = entry.commit
            entries.append({
                "sha": commit.id.decode(),
                "message": commit.message.decode(errors="replace").strip(),
            })
        return entries

    def revert(self, commit_sha: str) -> None:
        """回滚到指定 commit（检出该 commit 的文件内容，HEAD 同步指向该 commit）。"""
        repo = self._ensure_repo()
        # dulwich 1.2.12 无 checkout_tree/reset_hard：用 reset hard 等价实现
        dulwich.porcelain.reset(repo, "hard", commit_sha.encode())
