# paperflow/vision/

## Scope

- 本文件覆盖 `vision/` 视觉分析子包：PDF 图表区域检测、提取与多模态解读。

## Directory Structure

```text
vision/
├─ constants/      # enums.py：FigureType（图 / 表，取值即 pdffigures2 英文原型）
├─ schemas/        # 数据模型（一模型一文件）：figure.py 提取出的图表对象 + analysis.py 视觉模型结构化分析
├─ common/         # geometry.py：Box/Word/Line/Paragraph 几何原语（被解析、检测、模型、渲染横向消费）
├─ services/       # extractor.py(提取管线编排) + analyzer.py(看图分析) + renderer.py(图区栅格化)
├─ parsers/        # 管线各步解析：text_extractor / document_layout / caption / graphics
└─ detectors/      # figure_detector(图区定位) + region_classifier(区域分类)
```

包根只做再导出（`__init__.py`），公开名不变；每个子包的 `__init__` 也只做再导出。

## Core Rules

- **提取与看图两段式**：先 pdffigures2 式管线从 PDF 拿到图区候选（proposal 候选 + 打分选优 + no-overlap 互斥），再由视觉模型结构化看图分析；两段以 `schemas/` 的数据模型衔接。
- **两类消费方**：看图（`analyze_figures`，用 number/caption/image_bytes/mime）与 **造检索块**（RAG 索引侧用 caption/**image_words**（词 + 包围盒，表格重建要靠坐标分行列）/region_boundary/page，并传 `render_images=False` 跳过栅格化）——图与表都产出。
- **全链路降级**：视觉 api_key 缺失、页面无图、管线失败一律降级返回（不抛进 ReAct 循环），调用方拿到的是带原因的不可用结果。
- **视觉调用归属父轮次**：`analyze_figures` 工具（`tools/vision/`）`needs_parent=True`，视觉 LLM 调用计入父 agent 轮次进审计。
- **产物落盘**：分析结果与图片嵌入落盘后可供 `note-agent`（笔记图表节）与 `paper-agent` 引用，路径由工具参数指定。

## Key Entry Points

- `services/extractor.py` — 管线编排入口
- `services/analyzer.py` — 视觉模型调用（唯一出网点，走 `config.vision`）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 工具封装：[`../tools/AGENTS.md`](../tools/AGENTS.md)（vision/ 域）
