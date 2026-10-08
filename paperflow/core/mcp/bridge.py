"""桥接层：MCP 工具描述 → 原生 Tool。纯函数（命名/schema/分类/过滤）+ 适配器。

命名与规范化吸收 Letta（normalize_mcp_schema）与 OpenAI SDK（_safe_tool_name_part、
64 上限 + hash 后缀）的做法；写分类用 MCP annotations.readOnlyHint，缺注解按"可能写"
处理——MCP 规范明确 annotations 是不可信提示，缺省保守。风险姿态对齐业界：写类工具
可见但 requires_confirm 逐次确认，write_tools 预批准豁免，不默认隐藏。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from paperflow.config import McpServerConfig
from paperflow.core.mcp.constants import McpConnectionState
from paperflow.core.tool import Tool, ToolResult

#: OpenAI function name 上限：^[a-zA-Z0-9_-]{1,64}$
MAX_TOOL_NAME = 64


@dataclass
class McpToolSpec:
    """桥接前的 MCP 工具描述——从 SDK 类型剥离的最小面，单测不依赖 mcp 包。

    Attributes:
        name: str，MCP 工具名（server 原始名，未加前缀）
        description: str，工具描述（模型判断何时使用）
        input_schema: dict | None，MCP inputSchema 原文
        annotations: dict | None，MCP annotations（readOnlyHint 等，缺省按「可能写」处理）
    """

    name: str
    description: str
    input_schema: dict | None
    annotations: dict | None


def sanitize_name_part(s: str) -> str:
    """非 [a-zA-Z0-9_-] 字符替换为 '_'（OpenAI SDK _safe_tool_name_part 同款）。

    Args:
        s: str，待清洗的名字片段（server 名或工具名）

    Returns:
        非法字符替换为 '_' 后的字符串。
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def bridged_tool_name(server: str, tool: str) -> str:
    """mcp__<server>__<tool>；超 64 字符截断 + '_' + 8-hex sha1（先清洗再截断，同名稳定）。

    Args:
        server: str，MCP server 名
        tool: str，工具名

    Returns:
        mcp__<server>__<tool>；超 64 字符时截断并追加 8 位 sha1 后缀（同名稳定）。
    """
    base = f"mcp__{sanitize_name_part(server)}__{sanitize_name_part(tool)}"
    if len(base) <= MAX_TOOL_NAME:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return base[: MAX_TOOL_NAME - 9] + "_" + digest


def normalize_input_schema(schema) -> dict | None:
    """MCP inputSchema → OpenAI parameters；缺字段补齐，不可修复返回 None（调用方跳过该工具）。

    Args:
        schema: dict | None，MCP inputSchema 原文

    Returns:
        规范化后的 OpenAI parameters；不可修复时 None（调用方跳过该工具）。
    """
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
    """readOnlyHint=True → 只读；缺注解/非真 → 按"可能写"（保守默认）。

    Args:
        annotations: dict | None，MCP annotations

    Returns:
        True 表示按「可能写」处理（readOnlyHint 非 true 或缺注解）。
    """
    if not isinstance(annotations, dict):
        return True
    return annotations.get("readOnlyHint") is not True


def filter_tool_specs(cfg: McpServerConfig, specs: list[McpToolSpec]):
    """过滤顺序：allowed → disabled → schema 合法性。

    风险分级不隐藏工具——写类转为可见但 requires_confirm（见 build_mcp_tools），
    与业界「可见但审批」的做法一致。

    Args:
        cfg: McpServerConfig，该 server 的接入配置
        specs: list[McpToolSpec]，server 报告的工具

    Returns:
        (可见工具列表, 隐藏清单)；隐藏清单每项为 (工具名, 隐藏原因)。
    """
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
        visible.append(s)
    return visible, hidden


class McpToolAdapter(Tool):
    """桥接后的 MCP 工具：对 LLM/runtime 与原生 Tool 无差别；execute 投递后台循环。

    实例属性覆盖类级 name/description/parameters——tool_to_openai_schema 与
    validate_tool 都是实例属性读取，动态工具无需动态子类。

    Attributes:
        name: str，桥接后的工具名（mcp__server__tool）
        description: str，带 [来源] 标注的工具描述
        parameters: dict | None，规范化后的 JSON Schema
        risk_level: str，写类为 medium、只读为 low
        side_effects: list[str]，写类含 network+write_file，只读含 network
        output_scan: str，固定 "mark"（MCP 结果为外部内容）
        requires_confirm: bool，写类是否需逐次确认（write_tools 预批准时为 False）
        _manager / _server_name / _tool_name: McpClientManager 与目标（调用时投递给它）
    """

    def __init__(self, manager, server_name: str, spec: McpToolSpec, write_enabled: bool,
                 requires_confirm: bool = False):
        """按 MCP 工具描述装配适配器（覆盖类级 name/description/parameters）。

        Args:
            manager: McpClientManager，工具实际执行的投递目标
            server_name: str，所属 server 名
            spec: McpToolSpec，MCP 工具描述
            write_enabled: bool，是否按写类装配风险等级
            requires_confirm: bool，是否需要逐次用户确认
        """
        self.name = bridged_tool_name(server_name, spec.name)
        self.description = (
            f"{(spec.description or '').rstrip()}\n"
            f"[来源] MCP server {server_name}（工具 {spec.name}）；结果为外部内容。")
        self.parameters = normalize_input_schema(spec.input_schema)
        self.risk_level = "medium" if write_enabled else "low"
        # side_effects 只能用 tool.SIDE_EFFECTS 里的合法值（{write_file, delete_file,
        # network, read_file}），否则 validate_tool 抛 ValueError、AgentRegistry 装载即崩。
        self.side_effects = ["network", "write_file"] if write_enabled else ["network"]
        self.output_scan = "mark"
        # 写类默认可见但需逐次确认，write_tools 预批准豁免；readOnlyHint=true 只读
        # 工具自动放行（与业界「可见但审批」的做法一致，不默认隐藏）。
        self.requires_confirm = requires_confirm
        self._manager = manager
        self._server_name = server_name
        self._tool_name = spec.name

    def execute(self, **kwargs) -> ToolResult:
        """投递到后台循环执行 MCP 调用并把结果/异常统一转为文本。

        Args:
            kwargs: dict，模型给出的工具参数（原样转给 MCP）

        Returns:
            ToolResult；调用失败一律 is_error=True 的错误文本，绝不抛进 ReAct 循环。
        """
        # client.py 模块级反向 import bridge（McpToolSpec），此处延迟导入破循环。
        from paperflow.core.mcp.client import McpToolError, result_to_text
        try:
            result = self._manager.call_tool_sync(self._server_name, self._tool_name, kwargs)
            # 成功路径的转换也必须在 try 内：result_to_text 对畸形非文本内容
            # （如缺 model_dump 的项）会抛 AttributeError——"绝不抛进 ReAct 循环"
            # 是适配器的硬性不变量。
            text, is_error = result_to_text(result)
        except McpToolError as e:
            return ToolResult(text=f"MCP 工具调用失败：{e}", is_error=True)
        except Exception as e:
            return ToolResult(
                text=f"MCP 工具结果转换失败：{type(e).__name__}: {e}", is_error=True)
        return ToolResult(text=text, is_error=is_error)


def build_mcp_tools(server_name: str, cfg: McpServerConfig, manager) -> list[Tool]:
    """按过滤结果构造适配器列表；隐藏名单与需确认名单写回 ServerStatus 供 /mcp 展示。

    写类（readOnlyHint 非 true，含缺注解）可见但 requires_confirm=True，
    cfg.write_tools 预批准豁免；排序保持只读在前。

    Args:
        server_name: str，server 名
        cfg: McpServerConfig，接入配置
        manager: McpClientManager，状态与调用来源

    Returns:
        该 server 的工具适配器列表（未连接返回 []）；只读在前、写类在后。
    """
    st = manager.get_server_status(server_name)
    if st is None or st.status != McpConnectionState.CONNECTED:
        return []
    visible, hidden = filter_tool_specs(cfg, st.tools)
    st.hidden = hidden
    st.pending_confirm = [s.name for s in visible
                          if is_write_tool(s.annotations)
                          and s.name not in cfg.write_tools]
    reads = [s for s in visible if not is_write_tool(s.annotations)]
    writes = [s for s in visible if is_write_tool(s.annotations)]
    return ([McpToolAdapter(manager, server_name, s, write_enabled=False) for s in reads]
            + [McpToolAdapter(manager, server_name, s, write_enabled=True,
                              requires_confirm=(s.name not in cfg.write_tools))
               for s in writes])


def collect_mcp_agent_tools(agent_type: str, servers: dict[str, McpServerConfig],
                            manager) -> list[Tool]:
    """某 agent 可用的全部 MCP 工具（按各 server 的 agents 名单过滤）。

    逐 server：enabled 且 agents 包含该类型 → ensure_ready（受 connect_timeout 约束，
    失败跳过已告警）→ build_mcp_tools。未启动管理器/空配置自然返回 []。

    Args:
        agent_type: str，目标 agent 类型
        servers: dict[str, McpServerConfig]，全部 server 配置
        manager: McpClientManager，状态与调用来源

    Returns:
        该 agent 可用的全部 MCP 工具（未启用/不含该 agent/连接失败者跳过）。
    """
    tools: list[Tool] = []
    for name, cfg in servers.items():
        if not cfg.enabled or agent_type not in cfg.agents:
            continue
        if not manager.ensure_ready(name):
            continue
        tools.extend(build_mcp_tools(name, cfg, manager))
    return tools
