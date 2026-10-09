---
name: write-research-plan
description: 研究选题与计划的写作流程——从语料盘点到研究计划成稿，含盘点、survey/gaps 成稿、idea 卡、外部新颖性核查、方向确认与计划产出。触发：research-agent 收到「找研究方向 / 帮我选题 / 根据笔记定课题 / 梳理研究空白」的任务时，开工前先加载本流程。边界：素材不足即熔断，不硬凑方向、不编造证据。
metadata:
  version: "1.1.0"
  author: paperFlow
---

# Write Research Plan — 选题与计划的写作流程

你是被注入本流程的 research-agent，任务是从用户既有语料走到可执行的研究计划。

## 流程（严格按序）

1. **取课题**：任务文本带课题优先；没带 → **不猜**：按全库盘点给出候选方向，并把
   「需要用户定方向」写进最终结果（提问不是工具，本角色不能中途问用户）。
2. **盘点**：派 `rag-agent` 检索（一次可带多个检索式）+ 读素材，提炼已覆盖的主题 /
   候选论点 / 信息缺口——笔记里的「待验证想法」「局限」「关联文献」是主要素材。
3. **素材熔断**：相关笔记与 PDF 合并计数 < 3 篇，或提炼不出 ≥2 个候选方向 → **不硬凑**：
   一律自主补料——派 `paper-agent`（搜索并下载相关论文，原样带年份约束与下载动词）后
   重新盘点（补料 ≤2 轮），不向用户征询（提问不是工具，本角色不能中途问用户）。
   补后仍不足 → 返回「当前笔记积累不足以支撑选题，建议先积累以下方向：<缺口>」，不进入成稿。
4. **成稿 survey / gaps**：按模板成稿，survey 是语料主题地图，gaps 是缺口清单。

   ```
   load_skill(name="write-research-plan", resource="references/research_survey.md")
   load_skill(name="write-research-plan", resource="references/research_gaps.md")
   ```

5. **生成 idea 卡**：基于 gaps 出 3–5 张卡（名称与一句话主张 / 动机（Gap 来源，需溯源）/
   核心假设 / 验证思路 / interestingness 与 feasibility 打分）；对每张卡派 `paper-agent`
   检索最相似的已存在工作（源优先 semantic scholar，不下载），回填 similar_works 与判定
   （novel / not_novel / 需验证），并用 `edit_file` 同步 ideas.md。
6. **定方向**：任务已带课题 → 直接按它推进；没有 → 把 idea 卡摘要与你推荐的 1 个方向
   写进最终结果请用户定夺（提问不是工具，不能中途问完接着做），本轮到此为止。
7. **产出计划**：按模板产出 plan——研究问题 / 核心论点（2–3 个子主张，各带依据 + 推理 +
   风险）/ 论文结构草案 / 任务依赖图 / 证据规划表 / 预期对比对象与数据集 / 风险与 Plan B；
   正文末尾的参考文献派 `citation-agent` 渲染：

   ```
   spawn_sub_agent(agent_type="citation-agent",
                   task="按这些文献渲染参考文献：<标题或 key>…")
   ```

8. **诚实性检查**：产物声称基于 N 篇笔记 / PDF 时，确认这些素材真实存在且真的被引用；
   未经外部验证的方向不得写成已验证。

## 铁律

1. ⚠️ 产物落 `research` 根下的 `<slug>/` 目录，最终回复给出**绝对路径**。
2. ⚠️ 声称的素材必须真实存在；「未经外部验证」不得写成已验证。
3. ⚠️ 熔断即是结论——补料 ≤2 轮后仍不足就如实停下回报缺口（不硬凑方向、不编造证据）。
