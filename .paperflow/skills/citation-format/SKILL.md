---
name: citation-format
description: 论文引用格式转换与参考文献条目格式化。触发：用户要「转 GB/T 7714」「转 APA」「参考文献格式化」「引用格式统一」「按会议模板排版引用」。由 citation-agent 在格式化引用条目时加载（格式规则只归它的领域，其他角色不涉及）。纯文本变换，不检索、不下载、不改文件。
metadata:
  version: "1.1.0"
  author: paperFlow
# 收窄到它的使用者：引用渲染是 citation-agent 的领域，其余角色看不到这份规则
allowed_agents: [citation-agent]
---

# Citation Format — 论文引用格式转换

你是被注入本流程的 citation-agent，任务是按目标格式重排引用条目。

## 流程（严格按序）

1. 收集文献字段：题名 / 作者 / 年份 / 来源(期刊·会议·预印本) / 卷期页 / DOI。字段来自用户输入或上游任务文本，**缺什么记什么，绝不编造**。
2. 确定目标格式：GB/T 7714 → `load_skill(name="citation-format", resource="references/gb-t7714.md")`；APA → `resource="references/apa.md"`。未列出的格式如实说明不支持，不猜测规则。
3. 逐条转换：按格式卡的字段顺序与标点规则输出；无法映射的字段保留原值并在行尾注明 `[原文保留]`。
4. 输出：每条一行；条目数必须与输入一致；最后汇总「N 条中 M 条存在缺失字段」。

## 铁律

1. ⚠️ 缺失字段用「[缺]」占位，绝不编造作者/年份/卷期。
2. ⚠️ 不支持的目标格式明确拒绝，不用近似格式冒充。
