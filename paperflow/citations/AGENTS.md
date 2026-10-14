# paperflow/citations

## Scope

- 覆盖 `citations/` 引用管理子包（溯源落地）：`references.bib` 的读写、语料标题索引、引用键与渲染。
- 工具侧封装在 `tools/citations/`，只装配给 `citation-agent`。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：维护引用库真相源与引用溯源链——把论文解析成条目、生成稳定的引用键、按多种格式渲染，并核验产物里的引用标注。

## Directory Structure

```text
citations/
├─ constants/   # 解析状态与删除结局的取值
├─ schemas/     # 跨层数据模型：条目视图 + 解析结果
├─ storage/     # references.bib 的读写原语（真相源的唯一出入口）
├─ parsers/     # 书目提取：先联网取权威书目，取不到再由模型读首页兜底
└─ services/    # keys.py（引用键规则）+ corpus.py（语料标题索引/投影）+ manager.py（编排）
```

## Responsibilities

- 读写 `references.bib`：解析条目、去重、追加、按条目删除。
- 维护语料标题索引（易变投影，可随时重建）。
- 生成引用键、解析引用、按多种格式渲染，并支持「渲染视图回填空字段」的调和。

## Core Logic

- **`references.bib` 是唯一真相源，写入只有两种原语**：追加，与按条目原文块删除。两者都**不重新序列化其余内容**——用户手工维护的分节注释与未触碰条目要逐字节保留。为了「整理格式」而重写整个文件是禁止的。
- **corpus 投影的磁盘缓存必须读回**：缓存里的 mtime 表是「哪几篇已经索引过」的唯一判据，刷新时第一步就读回（缺失或损坏按冷启动）。不读回就会每次启动重读整库 PDF 首页、并重跑一遍联网书目提取；本轮无变更时也不要重写缓存文件。
- **标题与书目都不许回退到文件名**：标题来自 RAG 解析器的标题出口（判据宁空勿错）；书目（作者 / 年份 / 期刊）两级取——先联网取权威书目，取不到再由模型读首页兜底，**两级都失败就如实为空**。
- **分层惯例与 `core/rag/`、`core/memory/` 一致**：常量、数据模型、持久化原语、业务逻辑各占一层。`storage/` 里的读侧解析与写侧条目生成是一对，**必须同处**（拆开各自换家才叫割裂）。

## Key Entry Points

- `__init__.py` — 门面单例与对外 API（导入一律从包门面进）
- `services/manager.py` — 编排：解析 / 入库 / 删除 / 渲染 / 调和
- `storage/` — 条目扫描与读写原语（引用工具的底座）
- `services/corpus.py` — 语料标题索引

## Usage And Change Notes

- **渲染不回写**：渲染出的视图可以回填空字段（调和），但 bib 文件本身不动。
- **溯源标注契约**：笔记头部写 `**论文引用**: [key]`，节级论断标 `[来源:key§节]`。**核验时用查询工具确认 key 真实存在，不信任标注本身**——标注是产物，不是证据。
- **依赖许可约束（GPL 口径）**：书目首选方案（MIT）的依赖链里有 GPL-3.0 组件，本项目以关闭联网兜底的方式使其从不被调用。要彻底避开就得换自研标识符查找，实测代价见 ADR 0008——**改这条之前先读那份 ADR**。
- **对外只经 `get_citation_manager()`**：重组件（语料索引、书目提取器）首次使用才构造。

## Dependencies And Collaboration

- 消费方：`tools/citations/` 的六个引用工具；这些工具只装配给 `citation-agent`，其余角色（写笔记、做选题、做审查）需要时派发它。
- 依赖 `core/rag` 的标题解析出口；依赖 `core/llm` 做首页兜底抽取；书目提取需联网，出网经安全侧的统一下载出口。
- 与知识库（RAG 索引）的边界：本包只索引「标题」，不做全文检索。

## Investigation Rule

1. 先分清是「条目读错」还是「书目取不到」：前者看 `storage/`，后者看 `parsers/`。
2. 编排看 `services/manager.py`；读写原语看 `storage/`；引用键规则看 `services/keys.py`；语料索引看 `services/corpus.py`。
3. 排查「启动很慢」先查语料缓存是否被读回。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/citations -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/citations -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 设计依据：ADR 0008（引用管理）
- 工具封装：[`../tools/AGENTS.md`](../tools/AGENTS.md)

## References

- [`services/manager.py`](services/manager.py)
- [`storage/`](storage/)
- [`services/corpus.py`](services/corpus.py)
- [`parsers/`](parsers/)
