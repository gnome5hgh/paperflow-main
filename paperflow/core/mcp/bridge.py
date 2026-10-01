"""桥接层：MCP 工具描述 → 原生 Tool。纯函数（命名/schema/分类/过滤）+ 适配器。

命名与规范化吸收 Letta（normalize_mcp_schema）与 OpenAI SDK（_safe_tool_name_part、
64 上限 + hash 后缀）的做法；写分类用 MCP annotations.readOnlyHint，缺注解按"可能写"
处理——MCP 规范明确 annotations 是不可信提示，缺省保守（spec §5.3）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from paperflow.config import McpServerConfig
from paperflow.core.tool import Tool, ToolResult

#: OpenAI function name 上限：^[a-zA-Z0-9_-]{1,64}$
MAX_TOOL_NAME = 64


@dataclass
class McpToolSpec:
    """桥接前的 MCP 工具描述——从 SDK 类型剥离的最小面，单测不依赖 mcp 包。"""

    name: str
    description: str
    input_schema: dict | None
    annotations: dict | None


def sanitize_name_part(s: str) -> str:
    """非 [a-zA-Z0-9_-] 字符替换为 '_'（OpenAI SDK _safe_tool_name_part 同款）。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def bridged_tool_name(server: str, tool: str) -> str:
    """mcp__<server>__<tool>；超 64 字符截断 + '_' + 8-hex sha1（先清洗再截断，同名稳定）。"""
    base = f"mcp__{sanitize_name_part(server)}__{sanitize_name_part(tool)}"
    if len(base) <= MAX_TOOL_NAME:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return base[: MAX_TOOL_NAME - 9] + "_" + digest


def normalize_input_schema(schema) -> dict | None:
    """MCP inputSchema → OpenAI parameters；缺字段补齐，不可修复返回 None（调用方跳过该工具）。"""
    if schema is None:
        return {"type": "object", "properties": {}, "required": []}
    if not isinstance(schema, dict):
        return None
    out = dict(schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    out.setdefault("required", [])
    if out["type"] != "object" or not isinstance(out["properties"], dict):
        return None
    return out


def is_write_tool(annotations) -> bool:
    """readOnlyHint=True → 只读；缺注解/非真 → 按"可能写"（保守默认，spec §5.3）。"""
    if not isinstance(annotations, dict):
        return True
    return annotations.get("readOnlyHint") is not True


def filter_tool_specs(cfg: McpServerConfig, specs: list[McpToolSpec]):
    """过滤顺序（spec §4）：allowed → disabled → schema 合法性 → 风险分级。"""
    visible: list[McpToolSpec] = []
    hidden: list[tuple[str, str]] = []
    for s in specs:
        if cfg.allowed_tools is not None and s.name not in cfg.allowed_tools:
            hidden.append((s.name, "不在 allowed_tools 白名单"))
            continue
        if s.name in cfg.disabled_tools:
            hidden.append((s.name, "在 disabled_tools 黑名单"))
            continue
        if normalize_input_schema(s.input_schema) is None:
            hidden.append((s.name, "inputSchema 非法（不可修复），已跳过"))
            continue
        if is_write_tool(s.annotations) and s.name not in cfg.write_tools:
            hidden.append((s.name, "写类工具默认禁用（如需开启加入 write_tools）"))
            continue
        visible.append(s)
    return visible, hidden


class McpToolAdapter(Tool):
    """桥接后的 MCP 工具：对 LLM/runtime 与原生 Tool 无差别；execute 投递后台循环。

    实例属性覆盖类级 name/description/parameters——tool_to_openai_schema 与
    validate_tool 都是实例属性读取，动态工具无需动态子类（spec §5.2 定稿结论）。
    """

    def __init__(self, manager, server_name: str, spec: McpToolSpec, write_enabled: bool):
        self.name = bridged_tool_name(server_name, spec.name)
        self.description = (
            f"{(spec.description or '').rstrip()}\n"
            f"[来源] MCP server {server_name}（工具 {spec.name}）；结果为外部内容。")
        self.parameters = normalize_input_schema(spec.input_schema)
        self.risk_level = "medium" if write_enabled else "low"
        # 偏差：brief 原文为 ["network", "filesystem"]，但 "filesystem" 不在
        # tool.SIDE_EFFECTS（{write_file, delete_file, network, read_file}）里，
        # validate_tool 会抛 ValueError——AgentRegistry 装载即崩。最小修复取
        # 同义合法值 "write_file"。
        self.side_effects = ["network", "write_file"] if write_enabled else ["network"]
        self.output_scan = "mark"
        self._manager = manager
        self._server_name = server_name
        self._tool_name = spec.name

    def execute(self, **kwargs) -> ToolResult:
        # client.py 模块级反向 import bridge（McpToolSpec），此处延迟导入破循环。
        from paperflow.core.mcp.client import McpToolError, result_to_text
        try:
            result = self._manager.call_tool_sync(self._server_name, self._tool_name, kwargs)
        except McpToolError as e:
            return ToolResult(text=f"MCP 工具调用失败：{e}", is_error=True)
        text, is_error = result_to_text(result)
        return ToolResult(text=text, is_error=is_error)


def build_mcp_tools(server_name: str, cfg: McpServerConfig, manager) -> list[Tool]:
    """按过滤结果构造适配器列表；隐藏名单（含原因）写回 ServerStatus 供 /mcp 展示。"""
    st = manager.get_server_status(server_name)
    if st is None or st.status != "connected":
        return []
    visible, hidden = filter_tool_specs(cfg, st.tools)
    st.hidden = hidden
    reads = [s for s in visible if not is_write_tool(s.annotations)]
    writes = [s for s in visible if is_write_tool(s.annotations)]
    return ([McpToolAdapter(manager, server_name, s, write_enabled=False) for s in reads]
            + [McpToolAdapter(manager, server_name, s, write_enabled=True) for s in writes])
