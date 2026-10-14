# paperflow/core/intent

## Scope

- 覆盖 `core/intent/`：意图识别——**可选的预处理层**。总开关默认关；关掉时整套代码路径都是 no-op，提示词里也不出现意图痕迹。
- 本层只放子包，不放裸模块。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：**只做一件事**——把本轮输入归到一个类别，连同确定性实体一起注入根 agent 的 head。它不决定派发、不作门禁、不持跨轮状态。

## Directory Structure

```text
intent/
├─ constants/   # 类别枚举（词表唯一声明点）、知识库路径
├─ schemas/     # 产出契约与判定结果、知识库的数据模型
├─ rules/       # 实体抽取 + 知识库装载与 fail-closed 校验
└─ services/    # 判定服务客户端 + 集成缝（注入 head 的唯一钩子）
```

## Responsibilities

- 用两层判定产出单类别信号（+ 置信度、来源、确定性实体）。
- 装载并校验随仓库发布的意图知识库。
- 在根 agent 构造 head 时按需注入判定结果。

## Core Logic

- **两层，前一层不命中才进下一层**：规则层是纯代码、零成本、**永不猜测**（命中即定类，不命中就放行——没有分值也没有阈值）；判定服务层只在规则不命中时，由结合对话史的模型判定。**不要把规则层改成打分或加阈值**，那会把「高精度短路」变成一次调参。
- **判定必须看对话史，assistant 侧要保留**：判定口径是「判最后一条用户消息」，输入是最近若干轮 user / assistant 正文（不含工具结果）。掐掉 assistant 侧，「再下载一篇」这类指代就读不懂。
- **类别词汇只有一个声明点**：类别枚举即词表，知识库按它做 **fail-closed** 校验——类别缺条目、缺描述、缺示例、规则指向未知类别、模式非法正则、模式与示例逐字重复，都在启动期报错并**点名具体类别**。
- **只作信号**：产出注入 head 后就不再参与决策；派发、顺序、并行全归 supervisor 自己的 ReAct 循环。

## Key Entry Points

- `services/service.py` — 集成缝（注入 head 的唯一钩子）与规则块产出
- `services/jev.py` — 判定服务客户端
- `rules/entities.py`、`rules/taxonomy.py` — 实体抽取与知识库装载校验
- `schemas/intent.py` — 产出契约

## Usage And Change Notes

- **零保留被拒 = 这一层不可用**：网关拒绝零数据保留时按「该层不可用」处理，**不要摘掉该项重试**——要不要在无保留保证下把对话史发出去是隐私决定，不是一条降级策略。
- **判定服务不可用就整层不装配**：它依赖账户侧先完成绑定，启动探测失败（或密钥缺失）的后果是「意图层关闭」，**不是启动失败**，也不要留半套。
- **任何失败都退回规则层**，绝不抛进 ReAct 循环。
- 新增或改名类别要同时改枚举、知识库与校验三处。
- **知识库是随仓库分发的资产**（两份：判定口径与示例、高精度规则），不是运行期数据，也不跟随运行数据根重定向；不要在代码里另写一份类别说明。
- **旧实现不要复活**：混合路由器与稠密/稀疏编码、LLM 兜底面、跨轮状态机与追问检测、路由知识库与向量缓存都已整体退役。旧文档里的描述不作论据。

## Dependencies And Collaboration

- 消费方只有根 agent：由 CLI 装配时构造并注入，`None` 即「关」；**子 agent 一律不做意图识别**。
- 与 `tools/orchestration/` 零耦合：spawn 不读意图，也不要为它引入依赖。

## Investigation Rule

1. 先确认开关是否打开——意图层在本机默认是关的，先看清「没生效」是不是配置所致。
2. 集成缝与规则块看 `services/`；判定客户端看 `services/`；实体抽取与知识库校验看 `rules/`；数据模型看 `schemas/`。
3. 标定与评测题集在 `scripts/intent/calibration/`（不入库，删掉不可恢复）。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/intent -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/intent -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 设计依据：ADR 0007（意图识别）
- 知识库资产与入库规则：[`../../../.paperflow/AGENTS.md`](../../../.paperflow/AGENTS.md)

## References

- [`rules/`](rules/)
- [`services/`](services/)
- [`schemas/`](schemas/)
- [`../../../.paperflow/AGENTS.md`](../../../.paperflow/AGENTS.md)
