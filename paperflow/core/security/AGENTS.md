# paperflow/core/security

## Scope

- 覆盖 `core/security/`：工具调用的上下文对象、中间件协议与五个具体中间件、被工具与客户端直接调用的防护能力。
- 本层只放子包，不放裸模块。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：在进程内决定「这次工具调用要不要用户点头」，并把每次调用与每次模型调用记进审计。

## Directory Structure

```text
security/
├─ domain/dto/    # 调用上下文快照 + 审计事件快照（六类事件共用）
├─ middleware/    # base.py 协议与异常体系 + 审计落盘 + 路径权威
│                 #   + 内容防护 + 命令防护 + 策略引擎（唯一确认决策点）
└─ services/      # 被工具与客户端直接调用的防护能力：
                  #   统一出网出口与端点守卫、命令解析、可选语义判定
```

## Responsibilities

- 定义工具调用的上下文与中间件协议，串成 before / 执行 / after 的洋葱。
- 拦下不该读写的路径、不该进上下文的内容、不该执行的命令。
- 合并全部升级因后统一弹一次确认，并把决策落审计。

## Core Logic

- **本包只实现策略轴**：进程内决定「要不要用户点头」，性质是贴张纸，一个 bug 就能绕过。**隔离轴（边界由 OS 或容器强制）本项目不做**——别把这里的检查当隔离。
- **安全是中间件洋葱**：before（可拒 / 要求确认）→ 执行 → 逆序 after；整轮收尾跑 on_finish（可改写最终回答）。所有拦截降级为 `ToolResult` 文本，**绝不抛进 ReAct 循环**。
- **只有策略引擎弹确认**：其余中间件只把判定登记到上下文，由它合并升级因后统一弹一次。**不要在别的中间件里自行弹框**——那会同时破掉「问不问只有一个决策点」和审计里成对的审批事件。
- **确定性判据与语义判据分开**：不可见字符、伪装系统消息模板头、命令 argv 是确定性问题，只能由代码扫（模型看不见不可见字符）；模型只判语义诱导，且**只标不拦、失败 fail-open**。
- **路径检查与写盘共用同一份解析结果**：检查通过的路径参数就地归一化为解析后的绝对路径（防 TOCTOU）。

## Key Entry Points

- `middleware/base.py` — `SecurityMiddleware` 协议与异常体系
- `middleware/workspace.py` — 路径权威（读黑名单 + 写围栏 + 归一化）
- `middleware/policy_engine.py` — 唯一的确认决策点
- `middleware/audit.py` — 审计落盘
- `services/network.py` — 统一出网出口与端点守卫

## Usage And Change Notes

- **协议与实现同子包**：协议层与五个实现都在 `middleware/`——协议只服务它们，分开放只会让「谁定义契约」变含糊。
- **异常跟着协议或服务走**：安全异常家族与协议同处 `middleware/base.py`，出网异常跟 `services/network.py`。`domain/` 只放数据载体，不放行为。
- **本包不另立风险词汇**：风险等级与副作用取值在 `core/constants/`（枚举即单一真相源），从这里取，不要复制一份。
- **不要建 `constants/`**：扫描规则、不可见字符表、伪装系统消息模板、安全命令表、根目标表、私网段都只服务各自那一个中间件或服务，**就地声明**才对；真被两处消费了再搬。
- **未配对代理字符清洗不在这里**：它被多个包横向消费，已归 `core/common/services/text.py`——需要时从那里取，别在本包再实现一份。
- **中间件的装配顺序在 `paperflow/cli/services/assembly.py`**：顺序即执行顺序，改动顺序等于改动语义。

## Dependencies And Collaboration

- 被 `core/agent`（工具执行管道按洋葱顺序调中间件）、`core/llm`（端点守卫）、`core/rag` 与 `tools/*`（出网与命令解析）依赖。
- 依赖 `core/constants/`（风险与副作用词汇）与 `core/tool/`（工具的安全元数据声明）。
- 与终端的关系是反向的：确认框由 `terminal/confirm/` 消费，本包只产出确认请求。

## Investigation Rule

1. 先判断是「拦错了」还是「没拦住」：前者看对应中间件的判据，后者看策略引擎的升级因合并。
2. 路径问题看 `middleware/workspace.py`；内容问题看内容防护；命令问题看 `command_guard` 与 `services/command.py`；出网问题看 `services/network.py`。
3. 审计字段与格式看 `middleware/audit.py`；审计的实际落点是运行数据根下的 `security/audit/`（按天轮转的 JSONL）。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/security -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/security -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 设计依据：ADR 0002（安全设计）
- 确认框与终端侧：[`../../terminal/AGENTS.md`](../../terminal/AGENTS.md)

## References

- [`middleware/base.py`](middleware/base.py)
- [`middleware/workspace.py`](middleware/workspace.py)
- [`middleware/policy_engine.py`](middleware/policy_engine.py)
- [`services/network.py`](services/network.py)
