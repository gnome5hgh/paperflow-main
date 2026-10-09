---
name: review-note
description: 审笔记的流程——对笔记草稿做结构、保真、一致、完整与溯源五维审查并交裁决。触发：任务要求审阅一篇笔记草稿（通常带草稿路径与原文 PDF 路径）时，开审前先加载本流程。
metadata:
  version: "1.0.0"
  author: paperFlow
allowed_agents: [review-agent]
---

# Review Note — 笔记审查流程

裁决对象是**笔记草稿**。按下面的顺序核完再给裁决，不凭印象放行。

## 流程（严格按序）

1. `read_file` 读草稿。
2. `read_pdf` 读原文——论断要与原文对得上，不能只看草稿自洽。
3. `format_check` 查结构（标题树与模板比对）。
4. **核验溯源标注**：笔记头部 `**论文引用**: [key]` 与 `[来源:key§节]` →
   `list_citations(search=<key>)` 确认 key 真实存在于 references.bib；
   `[来源:笔记「X」§Y]` → `read_file` 读该笔记 §Y，确认内容支撑论断；
   `[⚠无支撑]` / `[待确认]` 未消除 → 如实列 blocking，不默认放行。
5. **沿链回溯**：论断与出处存疑时，按 `[来源:§X]` 回溯 `read_pdf` 对应章节核对原文。
6. **五维审查**：要求符合度 / 保真（含溯源核验）/ 内部一致 / 内容完整 / 结构完整。
7. `submit_review(path, verdict, issues)` 交裁决——**收尾必须调用**，不允许散文直接回复。

最终回复以「审查裁决:pass/fail」开头。

## 铁律

1. ⚠️ 溯源标注核验不通过（key 不存在 / 论断与出处不符 / `[⚠无支撑]` 未消除）→ **fail**，不默认放行。
2. ⚠️ verdict 与 issues 必须一致：pass 当且仅当无 blocking。
