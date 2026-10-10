# paperflow/citations/

## Scope

- 本文件覆盖 `citations/` 引用管理子包（溯源落地）；工具侧封装见 `tools/citations/`。

## Directory Structure

```text
citations/
├─ constants/   # 跨模块词汇：CitationStatus（解析状态）+ RemoveOutcome（删除结局）
├─ schemas/     # 数据模型（跨层共享）：BibEntry（条目视图）+ ResolvedCitation（解析结果）
├─ storage/     # references.bib 的读写原语——真相源的唯一出入口
│   └─ bib.py   # 原文解析（parse_entries/find_*）+ 条目文本生成（entry_text）+ append 追加 + 按条目原文块删除
├─ parsers/     # paper_meta_extract.py：pdf2bib 取作者/年份/期刊，失败再由 LLM 读首页兜底
└─ services/    # 业务层
    ├─ keys.py    # 引用键生成规则：{一作姓氏}{年份}{短标题}
    ├─ corpus.py  # 语料标题索引（易变投影）：PDF 解析标题 + 书目 → 全标题精确匹配，按 (path, mtime_ns) 增量重建（磁盘缓存读回）
    └─ manager.py # 编排：引用解析 → 入库 → 去重 → 渲染 → 调和
```

分层依据与 `rag/`、`core/memory/` 同惯例：常量、数据模型、持久化原语、业务逻辑各占一层；`bib.py` 的读侧 `parse_entries` 与写侧 `entry_text` 是一对（拆开各自换家才叫割裂，故同处 storage/）。

## Core Rules

- **references.bib 是唯一真相源，追加 + 按条目原文块删除**：写入只有 append 追加与按 key 删除命中条目原文块两种原语，都不重新序列化其余内容——用户手工维护的分节注释与未触碰条目逐字节保留（删除接缝两侧多余空行收敛为一个）；corpus 索引只是投影，可随时重建。
- **corpus 投影的磁盘缓存必须读回**：`corpus_titles.json` 的 mtime 表是「哪几篇已经索引过」的唯一判据，`refresh()` 首步读回（缺失/损坏按冷启动）；不读回就每次启动重读整库 PDF 首页、并重跑一遍书目提取（pdf2bib 联网取标识符与权威书目，整库要拖上几分钟）。无变更不重写缓存文件。
- **懒加载单例**：外部只经 `get_citation_manager()` 访问，重组件（corpus 索引、pdf2bib 书目提取器）首次使用才构造。
- **渲染不回写**：author-year/numbered/bibtex/gbt7714 四种格式渲染生成的视图可回填空字段（调和），但 bib 文件本身不动。
- **溯源标注契约**：笔记头部 `**论文引用**: [key]`，节级标注 `[来源:§X]`；reviewer 沿 `[来源:key§节]` 回溯时用 `lookup_citation` 核验 key 真实存在，不信任标注本身。
- **标题权威性**：标题来自 RAG 解析器的标题出口（元数据 + 首页版面，判据宁空勿错），绝不回退到 PDF 文件名；书目（作者/年份/期刊）由 `parsers/paper_meta_extract.py` 两级取：先经 pdf2bib（本地找 DOI/arXiv 标识符 → 联网取权威书目），取不到再由 LLM 读首页兜底；两级都失败即如实为空。

## Key Entry Points

- `__init__.py` — `get_citation_manager()` 单例与对外公开 API（导入一律从包门面进）
- `services/manager.py` — `CitationManager`：解析/入库/删除/渲染/调和的全部编排入口
- `storage/bib.py` — 条目扫描与读写原语（6 个引用工具的底座）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 工具封装：[`../tools/AGENTS.md`](../tools/AGENTS.md)（citations/ 域）
