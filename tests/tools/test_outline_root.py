# tests/tools/test_outline_root.py
"""outline 根注入：make_tools 把 outline 根解析进 allowed_paths。"""
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools.file.write_file import WriteFileTool


def test_write_file_allows_vault_outline_root(tmp_path):
    cfg = PaperFlowConfig(
        workspace=str(tmp_path / "ws"),
        vault_note_dir=str(tmp_path / "vault" / "note"),
        vault_outline_dir=str(tmp_path / "vault" / "outline"),
    )
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "vault" / "note").mkdir(parents=True, exist_ok=True)
    (tmp_path / "vault" / "outline").mkdir(parents=True, exist_ok=True)
    tools = make_tools(cfg, [WriteFileTool])
    assert str(tmp_path / "vault" / "outline") in tools[0].allowed_paths


def test_outline_root_falls_back_to_workspace(tmp_path):
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(exist_ok=True)
    tools = make_tools(cfg, [WriteFileTool])
    assert str(Path(cfg.workspace) / "outline") in tools[0].allowed_paths
