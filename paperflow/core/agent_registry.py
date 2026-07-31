# paperflow/core/agent_registry.py
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from paperflow.core.tool import Tool


@dataclass
class AgentConfig:
    name: str
    description: str = ""
    system_prompt: str = ""
    allowed_agents: list[str] = field(default_factory=list)
    allowed_spawns: list[str] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)


class AgentRegistry:
    def __init__(self, agents_dir: str = "agents"):
        self._agents: dict[str, AgentConfig] = {}
        self._discover(Path(agents_dir))

    def _discover(self, agents_dir: Path) -> None:
        if not agents_dir.is_dir():
            return
        for agent_path in sorted(agents_dir.iterdir()):
            if not agent_path.is_dir():
                continue
            skill_md = agent_path / "SKILL.md"
            if not skill_md.exists():
                continue
            meta, body = self._parse_skill_md(skill_md)
            name = meta.get("name", agent_path.name)
            tools = self._import_tools(agent_path / "tools.py")
            self._agents[name] = AgentConfig(
                name=name,
                description=meta.get("description", ""),
                system_prompt=body.strip() if body else meta.get("description", ""),
                allowed_agents=meta.get("allowed_agents", []),
                allowed_spawns=meta.get("allowed_spawns", []),
                tools=tools,
            )

    def _parse_skill_md(self, path: Path) -> tuple[dict, str]:
        text = path.read_text(encoding="utf-8")
        frontmatter = {}
        body = text
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
        if m:
            frontmatter = yaml.safe_load(m.group(1)) or {}
            body = m.group(2).strip()
        return frontmatter, body

    def _import_tools(self, tools_path: Path) -> list[Tool]:
        if not tools_path.exists():
            return []
        spec = importlib.util.spec_from_file_location(
            f"agent_tools_{tools_path.parent.name}", str(tools_path)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "TOOLS", [])

    def get_config(self, agent_type: str) -> AgentConfig:
        config = self._agents.get(agent_type)
        if config is None:
            raise KeyError(f"Unknown agent type: {agent_type}")
        return config

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())
