# paperflow/core/tool.py
from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass
class ToolResult:
    text: str
    summary: dict = field(default_factory=dict)


class Tool(ABC):
    name: str
    description: str
    parameters: dict
    risk_level: str = "low"

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        ...
