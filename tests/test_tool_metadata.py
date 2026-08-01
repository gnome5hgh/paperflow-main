# tests/test_tool_metadata.py
"""
Tool 安全元数据字段与 AgentRegistry 加载时校验的单元测试。

验证对象：
1. Tool 类的 5 个新安全字段默认值（risk_level 继承 Layer 0 已有声明）
2. SIDE_EFFECTS / RISK_LEVELS / RISK_ORDER 常量契约
3. 运行时自定义元数据
4. AgentRegistry._validate_tool 对非法值的拒绝与对合法值的接受
"""

import pytest

from paperflow.core.tool import (
    Tool, ToolResult, SIDE_EFFECTS, RISK_LEVELS, RISK_ORDER,
)


class MetaTool(Tool):
    name = "meta"
    description = "test tool"
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


def test_tool_defaults():
    t = MetaTool()
    assert t.risk_level == "low"
    assert t.side_effects == []
    assert t.requires_confirm is False
    assert t.blocked_by_default is False
    assert t.allowed_paths == []
    assert t.output_scan is None


def test_constants():
    assert SIDE_EFFECTS == {"write_file", "delete_file", "network", "read_file"}
    assert RISK_LEVELS == {"low", "medium", "high", "critical"}
    assert RISK_ORDER == {"low": 0, "medium": 1, "high": 2, "critical": 3}


def test_tool_custom_metadata():
    t = MetaTool()
    t.risk_level = "medium"
    t.side_effects = ["write_file"]
    t.requires_confirm = True
    t.allowed_paths = ["paper/note/"]
    t.output_scan = "mark"
    assert t.risk_level == "medium"
    assert t.output_scan == "mark"


# ─── AgentRegistry._validate_tool 校验测试 ──────────────────────────

from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.tool import RISK_LEVELS, SIDE_EFFECTS


class BadRiskTool(Tool):
    name = "bad_risk"
    description = "invalid risk"
    parameters = {"type": "object", "properties": {}}
    risk_level = "meduim"    # typo

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


class BadEffectsTool(Tool):
    name = "bad_effects"
    description = "invalid effects"
    parameters = {"type": "object", "properties": {}}
    side_effects = ["write_file", "delet_file"]  # typo

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


class BadScanTool(Tool):
    name = "bad_scan"
    description = "invalid output_scan"
    parameters = {"type": "object", "properties": {}}
    output_scan = "markk"  # typo

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


def test_validate_tool_rejects_bad_risk():
    with pytest.raises(ValueError, match="risk_level"):
        AgentRegistry._validate_tool(BadRiskTool())


def test_validate_tool_rejects_bad_effects():
    with pytest.raises(ValueError, match="side_effects"):
        AgentRegistry._validate_tool(BadEffectsTool())


def test_validate_tool_rejects_bad_output_scan():
    with pytest.raises(ValueError, match="output_scan"):
        AgentRegistry._validate_tool(BadScanTool())


def test_validate_tool_accepts_valid():
    AgentRegistry._validate_tool(MetaTool())  # 不应抛异常


# ─── allowed_roots 语义根声明测试（Layer 2）─────────────────────────

class RootTool(Tool):
    name = "root_tool"
    description = "declares semantic roots"
    parameters = {"type": "object", "properties": {}}
    allowed_roots = ["note", "pdf"]

    def execute(self) -> ToolResult:
        return ToolResult(text="ok")


def test_allowed_roots_default_empty():
    assert Tool.allowed_roots == []
    assert RootTool().allowed_roots == ["note", "pdf"]
