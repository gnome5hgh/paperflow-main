---
name: review-plan
description: 审研究选题产物的流程——对 survey/gaps/ideas/plan 四份产物交叉核验、溯源核验与素材熔断诚实性检查，裁决对象是 plan.md。触发：任务要求审阅选题产物（通常给出四个绝对路径）时，开审前先加载本流程。
metadata:
  version: "1.0.0"
  author: paperFlow
allowed_agents: [review-agent]
# references/ 是 write-research-plan 四份模板的副本——审查与写作要基于同一份模板，改的时候两份一起改。
---

# Review Plan — 选题产物审查流程

审查对象是四份产物（survey.md / gaps.md / ideas.md / plan.md）；**裁决对象是 plan.md**，
其余三份用于交叉核验。按下面的顺序核完再给裁决。

## 流程（严格按序）

1. `read_file` 读四产物全文（绝对路径由任务文本给出）。缺 plan.md → 如实报错；
   缺其余产物 → issues 标「产物缺失」（dimension=completeness），不默认放行。
2. **读模板**：`load_skill(name="review-plan", resource="references/research_plan.md")` ——
   这是裁决对象 plan.md 的验收标准，逐节核它要求写什么。交叉核验另三份前，各自读对应模板：
   `references/research_survey.md` / `research_gaps.md` / `research_idea.md`。
   四份都是 write-research-plan 模板的副本，**不要改这一份**——要改就改那份，两份一起同步。
3. **交叉核验**（plan ↔ 其余三份）：
   - plan 引用/对齐的 idea 卡 ↔ ideas.md（名称、一句话主张、新颖性判定一致）；
   - plan 动机 ↔ gaps.md（所依据的缺口真实存在且未被改写）；
   - survey 主题图 ↔ gaps 线索（抽查缺口确有语料线索支撑）。
4. **核验溯源标注**（适用四产物全部标注）：`[来源:key§节]` →
   `list_citations(search=<key>)` 确认 key 真实存在于 references.bib 且内容匹配；
   `[来源:笔记「X」§Y]` → `read_file` 读该笔记 §Y 确认支撑；`[⚠无支撑]` / `[待确认]`
   未消除 → 如实列 blocking，不默认放行。
5. **核验素材熔断诚实性**：产物声称基于 N 篇笔记 / PDF 时，确认这些素材真实存在且被引用；
   「未经外部验证」不得被写成已验证。
6. **五维审查**（按选题产物语义重诠释）：
   - requirements：课题覆盖（研究问题围绕所选方向、覆盖用户确认的范围）
   - faithfulness：映射真实性（「论点 ← 笔记」逐条核验）+ idea 卡 novelty 判定有
     similar_works 检索证据支撑（novel / not_novel 须有检索差异点）
   - consistency：四产物相互一致（plan ↔ ideas ↔ gaps ↔ survey 无矛盾）
   - completeness：四份产物各自覆盖其模板要求的章节（标准见本 skill 的 `references/`
     与第 2 步读到的模板）+ 四产物齐备
   - structure：逻辑（论点递进 / 依赖顺序合理）
7. `submit_review(path=plan_path, verdict, issues)` 交裁决，最终回复以「审查裁决:pass/fail」开头。

## 铁律

1. ⚠️ 溯源核验不通过或「素材熔断」声称与事实不符 → **fail**，不默认放行。
2. ⚠️ 裁决对象是 plan.md，但只看 plan 不读另外三份等于没交叉核验。
