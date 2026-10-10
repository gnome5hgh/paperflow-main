# paperflow/core/mcp/

## Scope

- 本文件覆盖 MCP 客户端平台：config 声明 server → 后台事件循环持久会话 → 逐工具桥接为原生 Tool。
- 本层只放子包（外加本 manifest 与 `__init__.py`），不放裸模块。

## Directory Structure

```text
mcp/
├─ constants/      # enums.py：McpConnectionState（pending / connected / failed）
├─ domain/dto/     # tool_spec.py（McpToolSpec 桥接前工具描述）+ server_status.py（ServerStatus 观测状态）
└─ services/       # client.py(自持后台事件循环 + 每 server 一条持久 ClientSession)
                   #   + bridge.py(逐工具桥接为原生 Tool：命名规范化 + allowed/disabled 过滤 + 风险分级)
```

## Core Rules

- **工具名恒为 `mcp__<server>__<tool>`**：前缀与规范化（非 `[a-zA-Z0-9_-]` 换 `_`）都在桥接层做；`MAX_TOOL_NAME=64` 上限超长时加 hash 后缀。
- **写类工具可见但需逐次确认**：按 `readOnlyHint` 分类，缺注解按「可能写」处理（MCP 规范明确 annotations 不可信）；`write_tools` 预批准豁免。不默认隐藏——可见性交用户判断。
- **连接失败不挡启动**：单个 server 连不上只跳过并记状态；调用失败重连一次，仍失败即以错误文本回传模型，**绝不抛进 ReAct 循环**。
- **每 server 一条持久会话，全部活在后台循环里**：runtime 的 `asyncio.to_thread(tool.execute)` 工作线程投递请求到后台循环执行，不阻塞 ReAct。
- **异常跟服务走**：`McpToolError` 留在 `services/client.py`（它描述一次调用失败，不是数据载体，不进 `domain/`）。
- **不要 `constants/` 之外的常量包**：连接状态枚举已收 `constants/`，工具名上限等只服务桥接层，就地声明。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 设计依据：ADR 0012（MCP 客户端平台）
- 工具装配缝：[`../AGENTS.md`](../AGENTS.md) 与 [`../../tools/AGENTS.md`](../../tools/AGENTS.md)（`merge_tools` 第 4 组）
