import pytest
from paperflow.core.memory.memory_index import MemoryIndex


class TestMemoryIndex:
    @pytest.mark.asyncio
    async def test_returns_empty_when_missing(self, tmp_path):
        idx = MemoryIndex(tmp_path)
        assert await idx.read() == ""

    @pytest.mark.asyncio
    async def test_reads_content(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("- [User](user_role.md) — role\n")
        idx = MemoryIndex(tmp_path)
        assert await idx.read() == "- [User](user_role.md) — role"

    @pytest.mark.asyncio
    async def test_truncates_to_200_lines(self, tmp_path):
        lines = [f"- line {i}" for i in range(250)]
        (tmp_path / "MEMORY.md").write_text("\n".join(lines))
        idx = MemoryIndex(tmp_path)
        out = await idx.read()
        assert len(out.splitlines()) == 200

    @pytest.mark.asyncio
    async def test_corrupt_file_returns_empty(self, tmp_path):
        (tmp_path / "MEMORY.md").write_bytes(b"\xff\xfe\x00 bad encoding")
        idx = MemoryIndex(tmp_path)
        assert await idx.read() == ""
