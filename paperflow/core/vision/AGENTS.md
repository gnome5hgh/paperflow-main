# paperflow/core/vision

## Scope

- 覆盖 `core/vision/`：PDF 图表区域的检测、提取与多模态解读。
- 本包的规则同时约束它的两个消费方——看图工具与 RAG 索引侧。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：从 PDF 里定位图与表，产出图区图像与结构化信息，并调用视觉模型解读。

## Directory Structure

```text
vision/
├─ constants/    # 图表类型枚举
├─ schemas/      # 提取出的图表对象 + 视觉模型的结构化分析
├─ common/       # 几何原语（框、词、行、段），被解析/检测/模型/渲染横向消费
├─ services/     # extractor.py（管线编排）+ analyzer.py（看图）+ renderer.py（栅格化）
├─ parsers/      # 管线各步解析：文本、版面、图注、图元
└─ detectors/    # 图区定位 + 区域分类
```

## Responsibilities

- 用 pdffigures2 式管线从页面拿到图区候选，做打分选优与互斥。
- 提供图区图像、图注、编号、坐标词等字段给消费方。
- 调视觉模型做结构化看图，并承担全链路降级。

## Core Logic

- **提取与看图是两段式**：先用管线拿到图区候选（proposal + 打分选优 + no-overlap 互斥），再由视觉模型结构化看图。两段之间只经 `schemas/` 的数据模型衔接，不要跨段直接调内部函数。
- **两类消费方，按需取字段**：看图方要编号、图注、图像字节与 mime；**造检索块**方要图注、坐标词（表格重建靠坐标分行列）、区域边界与页码，并跳过栅格化。新增字段时两个消费方都要过一遍。
- **全链路降级**：视觉 key 缺失、页面无图、管线失败一律返回**带原因的不可用结果**，绝不抛进 ReAct 循环。

## Key Entry Points

- `services/extractor.py` — 管线编排入口
- `services/analyzer.py` — 视觉模型调用（本包唯一出网点）
- `detectors/` — 图区定位与区域分类
- `common/geometry.py` — 几何原语

## Usage And Change Notes

- **视觉调用归属父轮次**：`analyze_figures` 工具声明 `needs_parent=True`，视觉 LLM 调用计入父 agent 轮次进审计——新增视觉调用点要保持这条归属关系。
- 改提取判据（什么算一张图、图注怎么认）会同时影响笔记侧的图表解读与 RAG 侧的媒体块，两边都要回归。
- 视觉 key 与模型走 `config.vision`；留空不崩启动，只在调用时降级。

## Dependencies And Collaboration

- 消费方一：`tools/vision/` 的看图工具（笔记与论文侧的图表解读）。
- 消费方二：`core/rag` 的索引侧（造媒体块，取坐标与图注，跳过栅格化）。
- 依赖 `core/llm` 做结构化输出；出网经安全侧的统一下载/请求出口。

## Investigation Rule

1. 先分清是「图区没找到」还是「看图失败」——前者在 `detectors/` 与 `parsers/`，后者在 `services/analyzer.py`。
2. 管线编排看 `services/extractor.py`；视觉调用看 `services/analyzer.py`；几何原语看 `common/`。
3. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/vision -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/vision -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 设计依据：ADR 0009（图片提取与多模态）
- 消费方：[`../../tools/AGENTS.md`](../../tools/AGENTS.md)、[`../rag/AGENTS.md`](../rag/AGENTS.md)

## References

- [`services/extractor.py`](services/extractor.py)
- [`services/analyzer.py`](services/analyzer.py)
- [`../rag/AGENTS.md`](../rag/AGENTS.md)
