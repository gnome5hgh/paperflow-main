# paperflow/vision/

## Scope

- 本文件覆盖 `vision/` 视觉分析子包：PDF 图表区域检测、提取与多模态解读。

## Directory Structure

```text
vision/
├─ extractor.py    # 提取管线编排：对齐 pdffigures2 的 8 步（文本→布局→图注→图形→分类→图检测→渲染）
├─ parsers/        # 管线各步解析：text_extractor / document_layout / caption / graphics
├─ detectors/      # figure_detector(图区定位) + region_classifier(区域分类)
├─ geometry.py     # Box 等几何原语
├─ renderer.py     # 图区栅格化成 PNG
├─ schemas.py      # 数据模型：提取出的图表对象 + 视觉模型结构化分析（pydantic）
└─ analyzer.py     # 图片+图注喂视觉模型 → FigureAnalysis（复用 StructuredOutput 三层防御）
```

## Core Rules

- **提取与看图两段式**：先 pdffigures2 式管线从 PDF 拿到图区候选（proposal 候选 + 打分选优 + no-overlap 互斥），再由视觉模型结构化看图分析；两段以 `schemas.py` 的数据模型衔接。
- **全链路降级**：视觉 api_key 缺失、页面无图、管线失败一律降级返回（不抛进 ReAct 循环），调用方拿到的是带原因的不可用结果。
- **视觉调用归属父轮次**：`analyze_figures` 工具（`tools/vision/`）`needs_parent=True`，视觉 LLM 调用计入父 agent 轮次进审计。
- **产物落盘**：分析结果与图片嵌入落盘后可供 noter/qa-agent 等引用，路径由工具参数指定。

## Key Entry Points

- `extractor.py` — 管线编排入口
- `analyzer.py` — 视觉模型调用（唯一出网点，走 `config.vision`）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 工具封装：[`../tools/AGENTS.md`](../tools/AGENTS.md)（vision/ 域）
