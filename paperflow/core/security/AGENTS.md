# paperflow/core/security/

## Scope

- 本文件覆盖安全子系统：工具调用的上下文对象、中间件协议与四个具体中间件、跨包共用的防护能力。
- 本层只放子包（外加本 manifest 与 `__init__.py`），不放裸模块。

## Directory Structure

```text
security/
├─ domain/dto/     # tool_context.py（ToolContext 一次工具调用的上下文快照）
│                  #   + audit_entry.py（AuditEntry 审计事件快照，五类事件共用）
├─ middleware/     # base.py(协议：SecurityMiddleware ABC + 异常体系 SecurityError/PolicyDenied/
│                  #   ConfirmRequired/SecurityBlocked) + audit.py(审计落盘) + workspace.py(路径边界)
│                  #   + scanner.py(输出扫描) + policy_engine.py(风险阈值与确认)
└─ services/       # network.py：SSRF 防护（validate_url_target / resolve_url_target），被 tools/ 直接调用
```

## Core Rules

- **安全是中间件洋葱**：`before`（可拒/要求确认）→ 执行 → 逆序 `after`；整轮收尾跑 `on_finish`（可改写最终回答）。所有拦截降级为 `ToolResult` 文本（`policy_denied`/`user_denied`/`auto_denied`/`security_blocked`），**绝不抛进 ReAct 循环**。
- **协议与实现同子包**：`middleware/base.py` 是协议层，四个实现也在 `middleware/`——协议只服务它们，分开放只会让「谁定义契约」变含糊。
- **异常跟着协议/服务走**：`SecurityError` 家族与协议同处 `middleware/base.py`；`SSRFError` 跟 `services/network.py`。`domain/` 只放数据载体。
- **路径与风险词汇已集中在上层**：风险等级/副作用取值在 `core/constants/`（枚举即单一真相源），本包不另立一套。
- **不要 `constants/`**：`SCAN_RULES` / `SHELL_COMMAND_RE` / `SENSITIVE_KEY_PATTERNS` / `PATH_KEYS` / `PRIVATE_NETS` 都只服务各自那个中间件，就地声明才对。
- **未配对代理字符清洗不在这里**：`sanitize_surrogates` 被 llm 客户端、记忆落盘、终端、rag 编码器横向消费，已归 `core/common/text.py`。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 设计依据：ADR 0002（安全设计）
- 装配顺序（四个中间件）见 [`../../AGENTS.md`](../../AGENTS.md) 的 Security middleware 节
