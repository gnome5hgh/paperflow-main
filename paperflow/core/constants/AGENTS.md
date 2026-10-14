# paperflow/core/constants

## Scope

- 覆盖 `core/constants/`：**跨模块词汇**——被两个以上子包消费的取值契约（风险等级、副作用、来源等）。
- 与 [`../common/constants/`](../common/constants/) 的分工：那里放**没有子包归属**的共享取值；这里放**跨模块的领域词汇**，它被安全、工具、装配三方共同消费。

## Module Positioning

- 角色：`bom-or-contract-leaf-manifest-dir`
- 职责：让「一个取值只有一处声明」在跨模块层面成立，成为安全判定与工具声明的共同词汇表。

## Directory Structure

- `enums/` — 一个枚举一个文件（风险等级、副作用、来源）
- `derived.py` — 由枚举派生的表（取值集合、排序等），供判定使用
- `__init__.py` — 只做再导出

## Responsibilities

- 声明跨模块共享的取值与它们的全集 / 排序。
- 供工具声明元数据、供安全中间件做判定、供注册表做加载期校验——三方读同一份。

## Core Logic

- **枚举即单一真相源**：消费方一律从这里取，**不要在别处复制一份字符串或列表**。复制的后果是「加了新档位，某一处不认」。
- **派生表也从枚举派生**：排序与全集不要手写第二遍，否则枚举一改就漂。
- **加载期就校验**：取值非法要在加载期 fail-fast，而不是留到运行期变成静默失效。
- **新增一个取值是一次跨模块变更**：要同时过「谁声明」「谁判定」「谁展示」三类消费方，且判定侧的排序表必须同步。

## Key Entry Points

- `enums/` — 各词汇的声明点
- `derived.py` — 派生表
- `__init__.py` — 再导出出口

## Usage And Change Notes

- 新增词汇前先确认它真的跨模块：只服务单一消费方的取值就地声明即可，**不要进这里**。
- 改枚举名或加取值时，先全仓检索消费方（安全判定、工具声明、注册表校验、审计落盘），改完再跑安全与工具两套测试。
- 落地顺序建议：装饰用（描述、展示）可以滞后，判定用（排序、白名单）必须同批改完。

## Dependencies And Collaboration

- 被 `core/security`（判定）、`core/tool`（声明与校验）、`paperflow/cli/`（装配期校验）消费。
- 本包不依赖任何其他子包——它是最底层的词汇表。

## Investigation Rule

1. 排查「档位/来源没生效」先确认消费方是从这里取的，而不是自己写了一份。
2. 加取值前先 grep 全仓的同名字符串，确认没有第二处声明。
3. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core tests/security -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core tests/security -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 消费方：[`../security/AGENTS.md`](../security/AGENTS.md)、[`../tool/AGENTS.md`](../tool/AGENTS.md)
- 另一类共享取值：[`../common/AGENTS.md`](../common/AGENTS.md)

## References

- [`enums/`](enums/)
- [`derived.py`](derived.py)
- [`../tool/AGENTS.md`](../tool/AGENTS.md)
