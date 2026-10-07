# paperflow/citations/

## Scope

- 本文件覆盖 `citations/` 引用管理子包（溯源落地）；工具侧封装见 `tools/citations/`。

## Directory Structure

```text
citations/
├─ bib.py       # references.bib 轻量扫描：条目查找/去重，不重写文件
├─ corpus.py    # 语料标题索引（易变投影）：note H1 + PDF 解析标题 → 全标题精确匹配，按 (path, mtime_ns) 增量重建
└─ manager.py   # 编排：引用解析 → 入库 → 去重 → 渲染 → 调和
```

## Core Rules

- **references.bib 是唯一真相源，append-only**：只追加、绝不重写，用户手工维护的分节注释原样保留；corpus 索引只是投影，可随时重建。
- **懒加载单例**：外部只经 `get_citation_manager()` 访问，重组件（corpus 索引、TitleExtractor）首次使用才构造。
- **渲染不回写**：author-year/numbered/bibtex/gbt7714 四种格式渲染生成的视图可回填空字段（调和），但 bib 文件本身不动。
- **溯源标注契约**：笔记头部 `**论文引用**: [key]`，节级标注 `[来源:§X]`；reviewer 沿 `[来源:key§节]` 回溯时用 `lookup_citation` 核验 key 真实存在，不信任标注本身。
- **标题权威性**：论文标题提取走 5 级回退链（搜索元数据 > GROBID > LLM > pdftitle > PyMuPDF 启发式），绝不回退到 PDF 文件名。

## Key Entry Points

- `manager.py` — `get_citation_manager()` 单例与全部编排入口
- `bib.py` — 条目扫描（4 个引用工具的底座）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 工具封装：[`../tools/AGENTS.md`](../tools/AGENTS.md)（citations/ 域）
