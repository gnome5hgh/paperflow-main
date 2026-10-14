# paperflow/core/common

## Scope

- 覆盖 `core/common/`：**跨子包共享的叶子能力与取值**——没有单一归属、又被两个以上子包消费的东西。
- 判据是「有没有第二个消费方」：只有一个消费方的就地声明，不要搬进来。

## Module Positioning

- 角色：`shared-package-manifest-dir`
- 职责：安放那些「谁都要用、但属于谁都不合适」的叶子实现与共享取值，避免同一份逻辑在多处各写一遍。

## Directory Structure

- `services/frontmatter.py` — 插件 manifest 的 frontmatter 解析（AGENT.md 与 SKILL.md 共用）
- `services/tokenization.py` — token 计数的单点实现
- `services/text.py` — 未配对代理字符清洗（跨包叶子）
- `constants/` — 无子包归属的共享取值（角色词等）

三个叶子进 `services/` 与 `core/security/services/` 同一口径——那里放的就是「被消费方直接调用的纯能力」。

## Responsibilities

- 提供被多处消费的纯函数与纯取值，不持状态、不依赖服务。
- 让「同一份逻辑只写一遍」在包级结构上成立。

## Core Logic

- **共享的判据是消费方数量，不是重要性**：被两个以上子包消费才进来；单个消费方的东西留在消费处——否则这里会退化成杂物间，谁都不敢删。
- **frontmatter 解析是同一条链的共用底座**：`AGENT.md`（agent 插件）与 `SKILL.md`（skill 插件）走同一份解析，改解析语义会同时影响两条加载链。
- **token 计数只有一个实现点**：它被压缩触发与上下文估算共用，两处各算一遍必然对不上。
- **文本清洗是跨包叶子**：未配对代理字符来自 PDF 抽取与外部文本，会在模型调用出站、记忆落盘、终端渲染、编码器输入多处炸——所以清洗实现放在这里，由各消费方在信任边界调用。
- **共享常量两级布局**：枚举放 `constants/enums/<name>.py`（一个枚举一个文件），其余放同层 `constants.py`；`constants/__init__.py` 只做再导出。这里的取值没有子包归属（例如角色词被消息层与记忆层同时消费），与 `core/constants/` 的跨模块词汇是两回事。

## Key Entry Points

- `services/frontmatter.py` — 解析入口
- `services/tokenization.py` — 计数入口
- `services/text.py` — 清洗入口
- `constants/` — 共享取值与枚举

## Usage And Change Notes

- 往里加东西前先确认消费方真的有两个以上；只有一个消费方的实现留在原处，**不要为了「整齐」搬过来**。
- 这里只放纯函数与数据：出现状态、IO、服务依赖就说明它放错了层。
- 清洗类实现要在**各自的信任边界**调用，不要指望某个下游替你兜——不同路径的编码失败点不一样。

## Dependencies And Collaboration

- 被 `core/` 多数子包消费（消息、记忆、终端、编码器、插件加载），也被 `tools/` 间接使用。
- 本包只依赖标准库与自己的 `constants/`，**不依赖任何其他子包**——这条保证它永远可以被安全地引用（它是最底层）。

## Investigation Rule

1. 想知道某段共享逻辑的唯一实现在哪，先看本包；被搬走过的东西通常在这里找到归宿。
2. 排查「两处算法不一致」先确认是否有一处绕过了这里的单点实现。
3. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 跨模块词汇（风险/副作用/来源）：[`../constants/AGENTS.md`](../constants/AGENTS.md)
- 插件加载方：[`../skills/AGENTS.md`](../skills/AGENTS.md)、[`../agent/AGENTS.md`](../agent/AGENTS.md)

## References

- [`services/frontmatter.py`](services/frontmatter.py)
- [`services/tokenization.py`](services/tokenization.py)
- [`services/text.py`](services/text.py)
- [`constants/`](constants/)
