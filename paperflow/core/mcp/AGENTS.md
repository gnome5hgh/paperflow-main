# paperflow/core/mcp

## Scope

- 覆盖 `core/mcp/`：config 声明 server → 后台事件循环里的持久会话 → 逐工具桥接为原生 Tool。
- 本层只放子包，不放裸模块。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：把外部 MCP server 的工具变成 agent 能直接看到的原生工具，并让它与本地工具走同一套安全与确认语义。

## Directory Structure

```text
mcp/
├─ constants/    # 连接状态枚举
├─ domain/dto/   # 桥接前的工具描述 + server 观测状态
└─ services/     # client.py（后台事件循环 + 每 server 一条持久会话）
                 #   + bridge.py（逐工具桥接：命名规范化、名单过滤、风险分级）
```

## Responsibilities

- 按配置连接 MCP server，发现其工具并桥接为原生工具。
- 维护连接状态与后台会话，向 REPL 提供各 server 的状态。
- 让写类工具走「可见但需逐次确认」的默认语义。

## Core Logic

- **工具名恒为固定前缀 + server + 工具名**：前缀与规范化（非法字符换下划线）都在桥接层做，且有长度上限、超长时加 hash 后缀。别在别处另拼一遍名字。
- **写类工具可见但需逐次确认**：按只读注解分类，**缺注解按「可能写」处理**（MCP 规范明确 annotations 不可信）；预批准名单里的才豁免。不默认隐藏——可见性交给用户判断。
- **每 server 一条持久会话，全部活在后台循环里**：工作线程投递请求到后台循环执行，不阻塞 ReAct。新增 MCP 交互时不要在工作线程里直接建连接。

## Key Entry Points

- `services/client.py` — 后台循环与持久会话
- `services/bridge.py` — 逐工具桥接与过滤
- `domain/dto/` — 工具描述与 server 状态

## Usage And Change Notes

- **连接失败不挡启动**：单个 server 连不上只跳过并记状态；调用失败重连一次，仍失败即以错误文本回传模型，**绝不抛进 ReAct 循环**。
- **异常跟服务走**：调用失败异常留在客户端实现里——它描述一次调用失败，不是数据载体，不进 `domain/`。
- **工具名上限这类只服务桥接层的常量就地声明**，不要为它建常量包；连接状态枚举才收 `constants/`。
- 桥接出来的工具在装配期并入 agent 工具表（顺序与分组见 `paperflow/cli/services/assembly.py`），不在本包决定给哪个 agent。

## Dependencies And Collaboration

- 被 `cli/` 在装配期调用（连接、发现、桥接、并入工具表）；被 REPL 的斜杠命令查询状态。
- 桥接出的工具与本地工具共用同一套安全元数据与确认机制——这是它不必自己实现安全的原因。
- 依赖 `config` 的 MCP server 声明（自由 dict，仅 YAML 可配）。

## Investigation Rule

1. 先分清是「连不上」还是「工具没出现」：前者看连接状态与配置，后者看桥接层的名单过滤与命名。
2. 后台循环与会话看 `services/client.py`；桥接与过滤看 `services/bridge.py`；配置声明看仓库根 `config.example.yaml`。
3. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/mcp -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/mcp -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 设计依据：ADR 0012（MCP 客户端平台）
- 工具装配缝：[`../../tools/AGENTS.md`](../../tools/AGENTS.md)

## References

- [`services/client.py`](services/client.py)
- [`services/bridge.py`](services/bridge.py)
- [`domain/dto/`](domain/dto/)
