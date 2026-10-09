---
name: review-note
description: 审笔记的流程——对笔记草稿做结构、保真、一致、完整与溯源五维审查并交裁决。触发：任务要求审阅一篇笔记草稿（通常带草稿路径与原文 PDF 路径）时，开审前先加载本流程。
metadata:
  version: "1.0.0"
  author: paperFlow
allowed_agents: [review-agent]
# references/ 是 write-note 模板的副本——审查与写作要基于同一份模板，改的时候两份一起改。
---

# Review Note — 笔记审查流程

裁决对象是**笔记草稿**。按下面的顺序核完再给裁决，不凭印象放行。

## 流程（严格按序）

1. `read_file` 读草稿。
2. `read_pdf` 读原文——论断要与原文对得上，不能只看草稿自洽。
3. **读模板**：`load_skill(name="review-note", resource="references/paper_note.md")`。
   模板就是验收标准——它规定了每一节该写什么（如 §2 的「设计决策(Why)」、来源分层的四类
   标签、核心主张的证据强度），**逐节拿这些要求去核，而不是只看标题在不在**。
   这份是 write-note 模板的副本，**不要改这一份**——要改就改 write-note 那份，两份一起同步。
4. `format_check` 查结构（标题树与模板比对）——它只比标题，内容是否达标由第 3 步的标准判。
5. **核验溯源标注**：笔记头部 `**论文引用**: [key]` 与各节 `[来源:key§节]` →
   **派 citation-agent 核 key**：`spawn_sub_agent(agent_type="citation-agent",
   task="核验这些 key 是否存在于 references.bib：<key1>、<key2>…")`——一篇笔记的
   key 凑一批派一次。它确认不存在的 key 未消除 → 如实列 blocking，不默认放行；
   `[来源:笔记「X」§Y]` → `read_file` 读该笔记 §Y，确认内容支撑论断；
   `[⚠无支撑]` / `[待确认]` 未消除 → 同样列 blocking。
6. **沿链回溯**：论断与出处存疑时，按 `[来源:§X]` 回溯 `read_pdf` 对应章节核对原文。
7. **五维审查**：要求符合度 / 保真（含溯源核验）/ 内部一致 / 内容完整 / 结构完整。
8. `submit_review(path, verdict, issues)` 交裁决——**收尾必须调用**，不允许散文直接回复。

最终回复以「审查裁决:pass/fail」开头。

## 铁律

1. ⚠️ 溯源标注核验不通过（key 不存在 / 论断与出处不符 / `[⚠无支撑]` 未消除）→ **fail**，不默认放行。
2. ⚠️ verdict 与 issues 必须一致：pass 当且仅当无 blocking。
