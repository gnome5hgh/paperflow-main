# tests/test_config.py
"""
PaperFlowConfig 配置加载测试。

覆盖 Layer 2 新增的 vault / RAG 六个配置键：
默认值、chroma_dir 推导、以及环境变量覆盖路径。
"""

from paperflow.config import PaperFlowConfig


def test_vault_dirs_default():
    c = PaperFlowConfig()
    assert c.vault_note_dir == "/Users/gnomeshgh/Documents/Obsidian Vault/paper/note"
    assert c.vault_pdf_dir == "/Users/gnomeshgh/Documents/Obsidian Vault/paper/pdf"


def test_chroma_dir_derives_from_workspace():
    c = PaperFlowConfig(workspace="data")
    assert c.chroma_dir == "data/chromadb"
    c = PaperFlowConfig(workspace="data", chroma_path="/custom/db")
    assert c.chroma_dir == "/custom/db"


def test_rag_keys_env_override(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_GROBID_URL", "http://localhost:9999")
    monkeypatch.setenv("PAPERFLOW_EMBED_MODEL", "BAAI/bge-small-en")
    monkeypatch.setenv("PAPERFLOW_RERANK_MODEL", "BAAI/bge-reranker-base")
    monkeypatch.setenv("PAPERFLOW_VAULT_NOTE_DIR", "/tmp/note")
    monkeypatch.setenv("PAPERFLOW_VAULT_PDF_DIR", "/tmp/pdf")
    monkeypatch.setenv("PAPERFLOW_CHROMA_PATH", "/tmp/db")
    c = PaperFlowConfig.from_env()
    assert c.grobid_url == "http://localhost:9999"
    assert c.embed_model == "BAAI/bge-small-en"
    assert c.rerank_model == "BAAI/bge-reranker-base"
    assert c.vault_note_dir == "/tmp/note"
    assert c.vault_pdf_dir == "/tmp/pdf"
    assert c.chroma_path == "/tmp/db"
