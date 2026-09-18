---
name: citation-format
description: 论文引用格式转换与参考文献条目格式化。触发：用户要「转 GB/T 7714」「转 APA」「参考文献格式化」「引用格式统一」「按会议模板排版引用」。纯文本变换，不检索、不下载、不改文件。
metadata:
  version: "1.0.0"
  author: paperFlow
allowed_agents: []   # 空 = 所有子 agent 可见
---

# Citation Format — 论文引用格式转换

你是被注入本 skill 的 agent，任务是按目标格式重排引用条目。

## 流程（严格按序）

1. 收集文献字段：题名 / 作者 / 年份 / 来源(期刊·会议·预印本) / 卷期页 / DOI。字段来自用户输入或上游任务文本，**缺什么记什么，绝不编造**。
2. 确定目标格式：GB/T 7714 → `load_skill(name="citation-format", resource="references/gb-t7714.md")`；APA → `resource="references/apa.md"`。未列出的格式如实说明不支持，不猜测规则。
3. 逐条转换：按格式卡的字段顺序与标点规则输出；无法映射的字段保留原值并在行尾注明 `[原文保留]`。
4. 输出：每条一行；条目数必须与输入一致；最后汇总「N 条中 M 条存在缺失字段」。

## 铁律

1. ⚠️ 缺失字段用「[缺]」占位，绝不编造作者/年份/卷期。
2. ⚠️ 不支持的目标格式明确拒绝，不用近似格式冒充。
