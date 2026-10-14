# .paperflow

## Scope

- 覆盖 `.paperflow/` 用户级数据目录。它同时是**两样东西**：Skill 插件的根目录（单级结构、内置与用户安装同层），以及**运行时数据根**的默认位置。
- 入库的只有静态资产：本 manifest、`skills/`、`intent/`；其余子目录都是运行期产物，已由 gitignore 覆盖。

## Module Positioning

- 角色：`asset-and-runtime-root-manifest`
- 职责：同时承载「要分发给谁」的静态资产（skill、意图知识库）与「跑起来才有」的运行期数据（记忆、审计、会话历史、索引状态、服务卷）。

## Directory Structure

```text
.paperflow/
├─ skills/         # Skill 插件唯一根目录（入库，有 manifest）；每份 skill 一个目录，目录名 = frontmatter 的 name
├─ intent/         # 意图知识库（入库，随仓库发布，有 manifest）
├─ memory/         # 记忆库与它的 markdown 投影
├─ security/       # 审计落盘（按天轮转的 JSONL）
├─ session/        # REPL 输入历史
├─ rag/            # 索引状态文件（带配方哈希）
├─ citations/      # references.bib 的默认落点
└─ infra/          # 依赖服务（Milvus / MinIO）的数据卷
```

## Responsibilities

- 作为 skill 与意图知识库的分发根，供运行时装载。
- 作为运行期数据根，收拢记忆、审计、会话历史、索引状态与服务卷。
- 用 gitignore 边界把「资产」与「产物」分开。

## Core Logic

- **只有静态资产入库**：往本目录下新增子目录时先想清楚它是不是运行期产物——是，就补进 gitignore；不是，才允许进版本库。
- **`security/` 与 `infra/` 受硬拦**：路径权威按前缀拦掉这两个目录的读写（审计防篡改、服务卷不许 agent 碰）。工作区根改到别处时，这两条保护要跟着配——改路径不补规则等于把门敞开。
- **意图知识库随仓库发布、路径锚仓库安装根**：它由装载器做 fail-closed 校验，**不随运行数据根的重定向而搬家**——它是内容资产，不是运行时数据。

## Key Entry Points

- `skills/` — skill 根目录（每份 skill 一个子目录）
- `intent/` — 意图知识库的两份文件（判定口径与示例、高精度规则）
- `memory/` — 记忆库与 markdown 投影
- `security/` — 审计落盘

## Usage And Change Notes

- **skill 的规范细节归 [`paperflow/core/skills/AGENTS.md`](../paperflow/core/skills/AGENTS.md)**：frontmatter 强制项、渐进披露、准入安装与 lock 治理都以那份 manifest 为准，本文件不重复。
- **运行期数据可以丢**：清空或重建下面的运行期目录只影响历史与索引，不改变任何代码事实。但它也不是事实源——排查时不要拿它当依据。
- 依赖服务的数据卷在 `infra/` 下；清空它会连图表原图一起清掉，重建后要重跑索引。
- `.agents/skills/` 与这里的 `skills/` **是两条不相干的线**：前者给编码代理用，后者是 paperFlow 运行时的能力包。

## Dependencies And Collaboration

- 被 `paperflow/core/skills/`（发现与安装）、`paperflow/core/intent/rules/`（装载知识库）、`paperflow/core/memory/`、`paperflow/core/security/middleware/audit.py`、`paperflow/core/rag/` 与 `paperflow/citations/` 消费。
- 运行数据根的位置由运行时配置的 `workspace` 决定，本目录是它的默认值。

## Investigation Rule

1. 先确认要查的是「资产」还是「产物」：资产看 `skills/` 与 `intent/`，产物只在用户明确要求排查时看。
2. skill 不生效看 [`paperflow/core/skills/AGENTS.md`](../paperflow/core/skills/AGENTS.md)；意图知识库装载报错看 `paperflow/core/intent/rules/`。
3. 运行数据根的具体子目录名以配置与存储实现为准，不要凭记忆猜。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core -k "skill or config" -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core -k "skill or config" -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- Skill 资产根与创作规则：[`skills/AGENTS.md`](skills/AGENTS.md)
- 意图知识库创作规则：[`intent/AGENTS.md`](intent/AGENTS.md)
- Skill 体系（装载与安装）：[`../paperflow/core/skills/AGENTS.md`](../paperflow/core/skills/AGENTS.md)
- 意图知识库装载：[`../paperflow/core/intent/AGENTS.md`](../paperflow/core/intent/AGENTS.md)

## References

- [`skills/`](skills/)
- [`intent/`](intent/)
- [`../paperflow/core/skills/AGENTS.md`](../paperflow/core/skills/AGENTS.md)
- [`../.gitignore`](../.gitignore)
