# REVIEW.md

代码审查的仓库定制层，供 cross-review（或任何审查流程）读取。只写审查特有约定——AGENTS.md 的硬红线已注入每个编码代理的上下文，此处不复述；与 AGENTS.md 冲突时以 AGENTS.md 为准。

## 重点盯防区域（按优先级）

| 区域 | 为什么 |
|---|---|
| `paperflow/core/security/` | 策略轴语义：唯一确认决策点、fail-safe 默认值、审计完整性。这里错 = 安全回归，且往往静默 |
| `paperflow/core/memory/` 的存储与并发语义 | SQL 是真相源：删 SQL 行、绕过持锁事务、拿摘要替换历史，都是静默数据损坏 |
| 装配层：`paperflow/cli/services/assembly.py`、工具注册表、`agents/*/tools.py` | 装配错 = 权限最小化被破（supervisor 拿到执行工具、领域工具装错人），且不报错 |
| 提示层契约：`agents/*/AGENT.md`、`.paperflow/skills/` | 改坏不报错、只静默失效；任何改动必须能说出「哪个行为会因此变化」 |

## 可跳过路径

`.paperflow/` 运行期产物、`.venv/`、`__pycache__/`、`config.yaml`（个人配置，gitignored；`config.example.yaml` 变更仍审）。

## spec 源

- `.scratch/<feature>/spec.md`（配套 `issues/NN-slug.md` 票据也算需求源）
- 实施期文档：`docs/superpowers/specs/` 与 `docs/superpowers/plans/`

与分支名或用户所指 feature 匹配者优先。

## 输出约束

- 总结以计数开头：X Important / Y Nit / Z Pre-existing。
- Nit 上限 5 条。
- 无 finding 明确写「无需修改」，不许凑数。
- 每条 finding 给 `文件:行号` 与失败场景；只凭符号名推断的不收。

## 风险分流（AI 审完后谁来把关）

| 档 | 范围 |
|---|---|
| 高——用户必须亲自看 | `core/security/`；`core/memory/` 存储与并发；装配层；`config/schemas/sections.py`；AGENTS.md / docs/adr / CONTEXT.md 本身的变更 |
| 中——用户抽查 diff | `core/` 其余；`agents/` 契约与 tools.py；`terminal/` |
| 低——AI 审过即可 | docs、注释与 docstring、`.agents/skills/`、`tests/`、`scripts/`、纯重命名/移动 |

## 反馈登记

审查反馈按四分类（已修 / 误报 / 有意设计 / 不值得修）追加到 `.scratch/review-feedback.md`；该 ledger 是修订本文件的依据。
