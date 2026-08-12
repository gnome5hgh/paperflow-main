# tests/core/test_config_outline_dir.py
"""vault_outline_dir 配置解析：env 覆盖 + 默认空。"""
from paperflow.config import PaperFlowConfig


def test_env_loads_vault_outline_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERFLOW_VAULT_OUTLINE_DIR", str(tmp_path / "outline"))
    cfg = PaperFlowConfig.from_env()
    assert cfg.vault_outline_dir == str(tmp_path / "outline")


def test_default_empty():
    cfg = PaperFlowConfig()
    assert cfg.vault_outline_dir == ""
