---
name: citation-format
description: 按目标格式（GB/T 7714、APA 等）重排参考文献条目，纯文本变换。触发：用户要求「转 GB/T 7714」「转 APA」「参考文献格式化」「引用格式统一」「按会议模板排版引用」，或 citation-agent 在格式化引用条目时。边界：不检索、不下载、不改文件；格式规则只归 citation-agent 的领域，其他角色不涉及。
metadata:
  version: "1.2.0"
  author: paperFlow
---

# Citation Format — 论文引用格式转换

你是被注入本流程的 citation-agent，任务是按目标格式重排引用条目。字段来自用户输入或上游任务文本。

## 流程（严格按序）

1. **收集字段**：题名 / 作者 / 年份 / 来源(期刊·会议·预印本) / 卷期页 / DOI。
   **缺什么记什么，绝不编造**。
2. **定目标格式**，按需读格式卡：

   ```
   GB/T 7714 → load_skill(name="citation-format", resource="references/gb-t7714.md")
   APA       → load_skill(name="citation-format", resource="references/apa.md")
   ```

   未列出的格式如实说明不支持，不猜测规则。
3. **逐条转换**：按格式卡的字段顺序与标点规则输出；无法映射的字段保留原值，并在行尾注明 `[原文保留]`。
4. **输出**：每条一行；条目数必须与输入一致；末尾汇总「N 条中 M 条存在缺失字段」。

## 铁律

1. ⚠️ 缺失字段用「[缺]」占位，绝不编造作者 / 年份 / 卷期。
2. ⚠️ 不支持的目标格式明确拒绝，不用近似格式冒充。
