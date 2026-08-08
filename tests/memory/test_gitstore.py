# tests/test_gitstore.py
from paperflow.core.memory.gitstore import GitStore


def make_store(tmp_path):
    return GitStore(tmp_path)


class TestGitStore:
    def test_commit_returns_none_when_no_changes(self, tmp_path):
        store = make_store(tmp_path)
        assert store.commit("no changes") is None

    def test_log_empty_repo_returns_empty(self, tmp_path):
        # 空仓库（无 commit）不得抛 KeyError，返回空列表
        store = make_store(tmp_path)
        assert store.log() == []

    def test_commit_and_log(self, tmp_path):
        store = make_store(tmp_path)
        (tmp_path / "user_role.md").write_text("hello", encoding="utf-8")
        sha = store.commit("first")
        assert sha is not None
        entries = store.log()
        assert entries[0]["message"] == "first"

    def test_commit_tracks_only_md_files(self, tmp_path):
        store = make_store(tmp_path)
        (tmp_path / "history.jsonl").write_text("{}", encoding="utf-8")
        (tmp_path / ".cursor").write_text("1", encoding="utf-8")
        (tmp_path / "user_role.md").write_text("x", encoding="utf-8")
        sha = store.commit("only md")
        assert sha is not None
        # 第二次只改非 md 文件 → 无变更 → None
        (tmp_path / "history.jsonl").write_text("{}2", encoding="utf-8")
        assert store.commit("no md change") is None

    def test_revert(self, tmp_path):
        store = make_store(tmp_path)
        (tmp_path / "user_role.md").write_text("v1", encoding="utf-8")
        sha1 = store.commit("v1")
        (tmp_path / "user_role.md").write_text("v2", encoding="utf-8")
        store.commit("v2")
        store.revert(sha1)
        assert (tmp_path / "user_role.md").read_text() == "v1"

    def test_commit_uses_author(self, tmp_path):
        store = make_store(tmp_path)
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        sha = store.commit("with author")
        import dulwich.porcelain
        repo = dulwich.porcelain.open_repo(str(tmp_path))
        commit = repo[repo.head()]
        assert commit.author == b"paperFlow <paperflow@local>"
