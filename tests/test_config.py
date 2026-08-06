# tests/test_config.py
"""
PaperFlowConfig 配置加载测试。

覆盖 Layer 2 新增的 vault / RAG 六个配置键：
默认值、chroma_dir 推导、以及环境变量覆盖路径。
"""

from pathlib import Path

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


def test_agent_timeouts_from_yaml(tmp_path):
    """agent_timeouts 经 config.yaml 顶层配置读入（D2，仅 YAML 无 env——dict 无自然 env 形态）。"""
    from paperflow.config import PaperFlowConfig
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("agent_timeouts:\n  generate-note: 300\n", encoding="utf-8")
    cfg = PaperFlowConfig.from_env(str(cfg_path))
    assert cfg.agent_timeouts == {"generate-note": 300}


def test_from_env_resolves_workspace_absolute(tmp_path):
    """RC1 回归：from_env 后 workspace 必须绝对——修复前相对 workspace 派生相对根，
    WorkspacePolicy 二次拼接成 data/data/templates 双前缀，正确绝对路径被拦。"""
    from paperflow.config import PaperFlowConfig
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("workspace: data\n", encoding="utf-8")
    cfg = PaperFlowConfig.from_env(str(cfg_path))
    assert Path(cfg.workspace).is_absolute()


def test_make_tools_roots_absolute_no_double_prefix(tmp_path):
    """RC1 端到端：from_env 相对 workspace 的配置 → _root_map 产出绝对根、无双前缀，
    且正确绝对模板路径能通过 WorkspacePolicy.check_path（修复前被 data/data 拦）。"""
    from paperflow.config import PaperFlowConfig
    from paperflow.tools.factory import _root_map
    from paperflow.core.security.workspace import WorkspacePolicy
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("workspace: data\n", encoding="utf-8")
    cfg = PaperFlowConfig.from_env(str(cfg_path))
    roots = _root_map(cfg)
    assert Path(roots["templates"]).is_absolute()
    assert "data/data" not in roots["templates"]
    template = Path(roots["templates"]) / "paper_note.md"
    assert WorkspacePolicy.check_path(str(template), [roots["templates"]])


def test_llm_config_official_limits():
    """deepseek-v4-flash 官方最大值：上下文 1M、输出 384K（max_tokens 合法范围 1-393216）。"""
    from paperflow.config import LLMConfig
    cfg = LLMConfig()
    assert cfg.context_window == 1000000
    assert cfg.max_tokens == 393216
