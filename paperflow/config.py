# paperflow/config.py
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    max_tokens: int = 4096
    temperature: float = 0.0


@dataclass
class PaperFlowConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    workspace: str = "data"
    agents_dir: str = "agents"

    @classmethod
    def from_env(cls, config_path: str | None = None) -> "PaperFlowConfig":
        load_dotenv()
        config = cls()
        config._load_yaml(config_path)
        config._load_env()
        return config

    def _load_yaml(self, config_path: str | None) -> None:
        path = Path(config_path or "config.yaml")
        if not path.exists():
            return
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if "llm" in data:
            for key, val in data["llm"].items():
                if hasattr(self.llm, key):
                    setattr(self.llm, key, val)
        for key in ("workspace", "agents_dir"):
            if key in data:
                setattr(self, key, data[key])

    def _load_env(self) -> None:
        env_map = {
            "PAPERFLOW_API_KEY": ("llm", "api_key"),
            "PAPERFLOW_BASE_URL": ("llm", "base_url"),
            "PAPERFLOW_MODEL": ("llm", "model"),
            "PAPERFLOW_WORKSPACE": (None, "workspace"),
            "PAPERFLOW_AGENTS_DIR": (None, "agents_dir"),
        }
        for env_var, (parent, attr) in env_map.items():
            val = os.getenv(env_var)
            if val:
                if parent == "llm":
                    setattr(self.llm, attr, val)
                else:
                    setattr(self, attr, val)
