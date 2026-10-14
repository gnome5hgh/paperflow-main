# paperflow/core/security/

## Scope

- 本文件覆盖安全子系统：工具调用的上下文对象、中间件协议与五个具体中间件、被工具与客户端直接调用的防护能力。
- 本层只放子包（外加本 manifest 与 `__init__.py`），不放裸模块。

## Directory Structure

```text
security/
├─ domain/dto/     # tool_context.py（ToolContext 一次工具调用的上下文快照）
│                  #   + audit_entry.py（AuditEntry 审计事件快照，六类事件共用：tool_started/
│                  #   tool_ended/approval_requested/approval_decided/agent_started/llm_call）
├─ middleware/     # base.py(协议：SecurityMiddleware ABC + 异常体系 SecurityError/
│                  #   ConfirmRequired/SecurityBlocked) + audit.py(审计落盘)
│                  #   + workspace.py(路径权威：读黑名单 + 写围栏 + 路径归一化)
│                  #   + content_guard.py(来源标记 + 确定性扫描 + 可选语义判定)
│                  #   + command_guard.py(命令解析与熔断；零声明即 no-op)
│                  #   + policy_engine.py(唯一的确认决策点)
└─ services/       # 被工具与客户端直接调用的防护能力：
                   #   network.py(统一出网出口：端点绑定 + SSRF + 目标 host)
                   #   command.py(命令解析与定档：语法树拆 argv)
                   #   classifier.py(语义判定层，可选、默认关、失败 fail-open)
```

## Core Rules

- **安全是中间件洋葱**：`before`（可拒/要求确认）→ 执行 → 逆序 `after`；整轮收尾跑 `on_finish`（可改写最终回答）。所有拦截降级为 `ToolResult` 文本（`user_denied`/`auto_denied`/`security_blocked`），**绝不抛进 ReAct 循环**。
- **协议与实现同子包**：`middleware/base.py` 是协议层，五个实现也在 `middleware/`——协议只服务它们，分开放只会让「谁定义契约」变含糊。
- **只有 PolicyEngineMiddleware 弹确认**：其余中间件（写围栏、命令防护）只把判定登记到 `ctx.escalations`，由它合并升级因后统一弹一次——保住「问不问只有一个决策点」与审计审批双事件的单一路径。新增升级原因时改那一个地方。
- **策略 vs 隔离**：本包只实现**策略轴**（进程内、决定要不要用户点头）。隔离轴（边界由 OS/容器强制）**本项目不做**（沙箱设计已移出主干，只留分支与调研材料）——读代码时别把策略当隔离。
- **异常跟着协议/服务走**：`SecurityError` 家族与协议同处 `middleware/base.py`；`SSRFError` 跟 `services/network.py`。`domain/` 只放数据载体。
- **路径与风险词汇已集中在上层**：风险等级/副作用取值在 `core/constants/`（枚举即单一真相源），本包不另立一套。
- **不要 `constants/`**：`SCAN_RULES` / `INVISIBLE_CHARS` / `FAKE_SYSTEM_RE` / `SAFE_COMMANDS` / `ROOT_TARGETS` / `PRIVATE_NETS` 都只服务各自那个中间件或服务，就地声明才对。
- **外部内容用确定性判、语义交模型**：不可见字符、伪装系统消息模板头、命令 argv 都是**确定性**问题，只能由代码扫（模型看不见不可见字符）；模型只判语义诱导，且**只标不拦、失败 fail-open**。
- **未配对代理字符清洗不在这里**：`sanitize_surrogates` 被 llm 客户端、记忆落盘、终端、rag 编码器横向消费，已归 `core/common/text.py`。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 设计依据：ADR 0002（安全设计）
- 装配顺序（五个中间件）见 [`../../AGENTS.md`](../../AGENTS.md) 的 Security middleware 节
