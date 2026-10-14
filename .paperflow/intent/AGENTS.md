# paperflow/.paperflow/intent

## Scope

- 覆盖 `.paperflow/intent/`：意图识别**知识库**——随仓库分发的资产，不是运行期数据，也不随运行数据根重定向。
- 装载与校验在代码侧，见 [`../../paperflow/core/intent/AGENTS.md`](../../paperflow/core/intent/AGENTS.md)。

## Module Positioning

- 角色：`asset-root-manifest-dir`
- 职责：用数据（而不是代码改动）承载「什么话算哪一类」——判定服务拿它当判据，规则层拿它的高精度模式做零成本短路。

## Directory Structure

- `taxonomy.yaml` — 每类的判定口径与示例句；口径与示例合起来构成判定模型的判据
- `rules.yaml` — 高精度模式（命中即定类），每条带说明
- `README.md` — 面向读者的说明

## Responsibilities

- 提供类别之间的边界（难点在边界，不在定义）。
- 提供一批「命中即定类、不命中就放行」的高精度模式。
- 让改判定口径变成改数据，不必动代码。

## Core Logic

- **两份文件分工明确**：判定口径与示例服务于模型判定（模糊地带靠它）；高精度模式服务于规则层短路（只在有把握时命中）。
- **口径要写「是什么 / 不包什么」**：只说「是什么」会在相邻类别之间留下重叠，边界才是判定出错的地方。
- **规则层永不猜测**：模式必须高精度，命中即定类、不命中就交给下一层——没有分值也没有阈值，所以不要写「大致像」的模式。
- **类别词汇的唯一声明点在代码里**：本目录按它做 **fail-closed** 校验，写错会被启动期拒掉（并点名具体类别），所以维度的权威在枚举，本目录是它的数据面。

## Key Entry Points

- `taxonomy.yaml` — 口径与示例
- `rules.yaml` — 高精度模式
- `README.md` — 目录说明

## Usage And Change Notes

- **新增或改名类别要同步三处**：代码里的类别枚举、本目录的两份文件。枚举是校验的基准，漏改会启动即报错。
- **示例不能与规则逐字重复**：校验会拒（这种重复意味着你把规则抄成了样例，没有独立信息）。
- 模式写宽会误命中（哪怕看起来「差不多」），写窄只是不短路——**宁窄勿宽**，因为错分类比走模型判定更贵。
- 标定与评测题集在 `scripts/intent/calibration/`（不入库，删掉不可恢复）；改口径后要重跑标定，别只看单条例子。

## Dependencies And Collaboration

- 被 `paperflow/core/intent/rules/` 装载并做 fail-closed 校验。
- 与代码的分工：**词汇与校验在代码，口径与模式在本目录**——改判定行为通常只动这里。

## Investigation Rule

1. 「这句话为什么被判成那一类」先看 `taxonomy.yaml` 的口径与示例，再看 `rules.yaml` 有没有短路。
2. 启动报知识库错误时按报错点名的类别定位到具体条目。
3. 改完记得确认校验能过（启动即校验），以及标定题集是否需要重跑。
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

- 上级：[`../AGENTS.md`](../AGENTS.md)（.paperflow manifest）
- 装载、校验与判定：[`../../paperflow/core/intent/AGENTS.md`](../../paperflow/core/intent/AGENTS.md)

## References

- [`taxonomy.yaml`](taxonomy.yaml)
- [`rules.yaml`](rules.yaml)
- [`README.md`](README.md)
- [`../../paperflow/core/intent/AGENTS.md`](../../paperflow/core/intent/AGENTS.md)
