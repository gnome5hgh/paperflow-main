---
name: review-download
description: 下载与推荐前门禁——对候选论文清单（紧凑 JSON）逐篇核验年份、等级（仅当用户要求）、相关性与可下载性，产出通过清单。触发：任务给出候选论文清单与用户约束、要求出门禁裁决时，开审前先加载本流程。边界：只核验与裁决，不下载、不改文件。
metadata:
  version: "1.1.0"
  author: paperFlow
---

# Review Download — 下载 / 推荐前门禁流程

你是被注入本流程的 review-agent。任务含**候选论文清单**（紧凑 JSON：标题 / 年份 / venue /
issn / pdf_url / 来源）与**用户约束**——约束由 paper-agent 从用户请求提炼，通常含年份、主题；
等级**仅当用户明确要求**才出现。逐篇核验，**按任务中实际出现的约束驱动**，不是固定四维。

## 流程（严格按序）

1. **年份**（任务含年份约束时）：元数据 year ≥ 约束年份；缺 year → fail。
2. **等级**（仅当任务含「等级≥X」时核验）：`lookup_venue_rank(venue, issn)` 查等级后判定：

   | 情况 | 判定 |
   |---|---|
   | 期刊 JCR Q1/Q2 或中科院一/二区 | 通过 |
   | 会议 CCF-A/B | 通过 |
   | 预印本（venue 为空） | fail（预印本无期刊等级） |
   | 等级未找到 | fail（不默认通过） |

   **任务不含等级约束 → 跳过本维度**：预印本、未找到等级、低等级期刊均不因等级 fail。
3. **相关性**：判断是否属于用户主题。
4. **可下载性**：pdf_url / `downloadable` 是否可用。

**多篇等级查询**：`lookup_venue_rank` 在**同一轮并行调用**（一次发多篇，网络等待并发，省墙钟；
每篇独立判定，互不等待）。

5. **交裁决**：

   ```
   submit_download_review(verdict, items)
   ```

   每条 items 含 title / decision(pass|fail) / reasons[] / source_link；venue_rank 仅在查过等级时带上。
   最终回复以「审查裁决:pass/fail」开头，复述 pass 清单与每项理由。

## 铁律

1. ⚠️ 有等级要求时等级未找到 → **fail，不默认通过**（宁缺毋滥）。
2. ⚠️ 「等级」「可下载性」都不信任上游字段，自己查 / 自己判。
