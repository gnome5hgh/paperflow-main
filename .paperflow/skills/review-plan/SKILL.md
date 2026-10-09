---
name: review-plan
description: 审研究选题产物的流程——对 survey/gaps/ideas/plan 四份产物交叉核验、溯源核验与素材熔断诚实性检查，裁决对象是 plan.md。触发：任务要求审阅选题产物（通常给出四个绝对路径）时，开审前先加载本流程。
metadata:
  version: "1.0.0"
  author: paperFlow
allowed_agents: [review-agent]
---

# Review Plan — 选题产物审查流程

审查对象是四份产物（survey.md / gaps.md / ideas.md / plan.md）；**裁决对象是 plan.md**，
其余三份用于交叉核验。按下面的顺序核完再给裁决。

## 流程（严格按序）

1. `read_file` 读四产物全文（绝对路径由任务文本给出）。缺 plan.md → 如实报错；
   缺其余产物 → issues 标「产物缺失」（dimension=completeness），不默认放行。
2. **交叉核验**（plan ↔ 其余三份）：
   - plan 引用/对齐的 idea 卡 ↔ ideas.md（名称、一句话主张、新颖性判定一致）；
   - plan 动机 ↔ gaps.md（所依据的缺口真实存在且未被改写）；
   - survey 主题图 ↔ gaps 线索（抽查缺口确有语料线索支撑）。
3. **核验溯源标注**（适用四产物全部标注）：`[来源:key§节]` →
   `list_citations(search=<key>)` 确认 key 真实存在于 references.bib 且内容匹配；
   `[来源:笔记「X」§Y]` → `read_file` 读该笔记 §Y 确认支撑；`[⚠无支撑]` / `[待确认]`
   未消除 → 如实列 blocking，不默认放行。
4. **核验素材熔断诚实性**：产物声称基于 N 篇笔记 / PDF 时，确认这些素材真实存在且被引用；
   「未经外部验证」不得被写成已验证。
5. **五维审查**（按选题产物语义重诠释）：
   - requirements：课题覆盖（研究问题围绕所选方向、覆盖用户确认的范围）
   - faithfulness：映射真实性（「论点 ← 笔记」逐条核验）+ idea 卡 novelty 判定有
     similar_works 检索证据支撑（novel / not_novel 须有检索差异点）
   - consistency：四产物相互一致（plan ↔ ideas ↔ gaps ↔ survey 无矛盾）
   - completeness：plan 模板章节覆盖（研究问题 / 核心论点 / 论文结构 / 任务图 / 证据规划 /
     风险）+ 四产物齐备
   - structure：逻辑（论点递进 / 依赖顺序合理）
6. `submit_review(path=plan_path, verdict, issues)` 交裁决，最终回复以「审查裁决:pass/fail」开头。

## 铁律

1. ⚠️ 溯源核验不通过或「素材熔断」声称与事实不符 → **fail**，不默认放行。
2. ⚠️ 裁决对象是 plan.md，但只看 plan 不读另外三份等于没交叉核验。
