---
name: cross-review
description: 深度多 agent 代码审查——并行多个 subagent 独立找 bug，每条发现经独立 verifier 交叉核实过滤误报，按 Important/Nit/Pre-existing 分档出报告。审「当前分支 vs main 的 merge-base」（默认）、任意固定点（commit/branch/tag）、或工作树未提交改动（wip）。自动读取仓库根 REVIEW.md 的定制层（重点区域/输出约束/风险分流），没有则用默认。用户要求审分支、审改动、找 bug、深度审查、多 agent 审查、交叉核实、修复后复审时使用；需要 Standards/Spec 两轴逐条对照的细审用 code-review。
---

# Cross-Review：多 agent 交叉核实审查

机制来源：Anthropic《Solving code review at Anthropic》——多 agent 并行找 bug、独立 agent 交叉核实、人的注意力留给高风险决策。本 skill 是它的单人本地版：**审查到报告为止**，修不修由人决定。

总流程：固定范围解析 → 定制层加载 → 并行 finder → 去重 → 逐条 verifier → 分档报告 + 反馈登记。

## 1. 固定范围

三种模式，先定模式再动手：

- **分支审（默认）**：固定点缺省为 `main` 的 merge-base，即 `git diff main...HEAD`；用户点名其他固定点（commit/branch/tag）则用之。同时取 commit 清单 `git log <固定点>..HEAD --oneline`。
- **wip 审**：审工作树未提交改动 `git diff HEAD`（含暂存），用于大改动中途早发现问题。
- **复审**：用户修复后说「复审」触发，见第 6 节。

预检（失败在此停下，不进 subagent）：固定点能被 `git rev-parse` 解析；diff 非空。

分支模式下若 diff 触及 REVIEW.md 本身，审查以**固定点侧**的 REVIEW.md 为准——防审查规则被改动自我豁免。

## 2. 加载定制层

- 仓库根 `REVIEW.md`：有则全程遵守（重点盯防区域、可跳过路径、输出约束、风险分流表、**spec 源声明**）。**没有也照常跑**，用以下内建默认：
  - 重点盯防区域：无清单，全 diff 平扫；约定依据用已注入上下文的 AGENTS.md / CLAUDE.md，两者都没有就退到通用工程判断；
  - 输出约束：计数开头总结、Nit ≤ 5、无 finding 明确写「无需修改」、必须 `文件:行号` 证据、丢弃风格/诊断/清理类建议；
  - 风险分流：报告省略「该亲自看哪部分」提示；
  - 反馈登记：追加到 `.scratch/review-feedback.md`（目录不存在先建）；
  - 收尾可顺带问用户：要不要按本仓情况起草一份 REVIEW.md。
- spec 源（供 spec-checker）按序解析：① 用户在本轮输入里点名的路径；② REVIEW.md 声明的 spec 源（各仓存放约定不同，写在这里）；③ 内建默认——`.scratch/` 下与分支名或当前 feature 匹配的 spec、`specs/`、`docs/` 下的 spec/plan 文档；④ 仍没有就问用户一句「spec 在哪，还是没有」，答没有则 spec 轴跳过，报告里注明「无 spec 可对照」。
- 仓库的代理指令文件（AGENTS.md / CLAUDE.md）通常已注入你的上下文——它是约定类 finding 的依据，不必重读。

## 3. 并行 finder

单条消息并行派发（用所在客户端的原生 subagent 机制）：

- **3 个 bug finder**：各自独立扫全 diff 及周边代码。**不分工、不给不同侧重**——独立性是机制的一部分：同一问题被多个 finder 独立发现即互相印证。
- **1 个 spec-checker**（有 spec 才派）：对照 spec 找四类偏差——要求缺失、部分实现、做了没要的（scope creep）、看似实现实则不对。

finder 的 prompt 必须包含：diff 命令与 commit 清单；REVIEW.md 全文（若有）；以下输出规则：

- 每条 finding 给全：类型（bug / convention / spec）、候选档位（Important / Nit）、`文件:行号`、问题描述、失败场景推演、证据（bug 引代码，convention 引规则原文，spec 引 spec 行）。
- 丢弃纯风格/诊断/清理类建议（加日志、可重构、命名偏好），除非 REVIEW.md 明确要。
- 禁止凭符号名推断问题——必须定位到文件与行号，给不出定位就不报。
- 改动只是暴露了旧代码已有的问题 → 标 pre-existing。
- 直接输出 findings 列表，不写寒暄与过程叙述。

## 4. 逐条 verifier

汇总全部 findings 去重（同文件同主题合并，记录每条被几个 finder 独立发现），然后**每条派一个 fresh-context verifier**（同文件的相邻 findings 可合并给一个 verifier 省开销）：

- 只给 finding 全文与相关 diff 位置提示，**不给 finder 的数量与身份**——防锚定。
- 核实方式按类型：bug → 读当前代码推演该失败场景是否真实成立；convention → 与规则原文逐字比对；spec → 与 spec 原文比对。
- 裁决三值：confirmed / false-positive / pre-existing，各附一句依据。
- 只有 confirmed 进正式报告；false-positive 丢弃（计数留给总结）；pre-existing 进附录节。

## 5. 报告

- 首行计数总结：`X Important / Y Nit / Z Pre-existing（W 条误报被核实过滤）`。
- **Important**：每条 文件:行号 + 问题 + 失败场景 + 依据。
- **Nit**：上限默认 5（REVIEW.md 可改），超出只留最值得修的。
- **Pre-existing**：一行一条——不是本次改动引入，但值得知道。
- 没有 finding 就明确写「无需修改」，不许凑数。
- 若 REVIEW.md 有风险分流表：标注本单涉及的最高档区域，提示用户该亲自看哪部分。
- 报告末尾附**反馈登记区**：逐条列出 finding 编号，请用户裁决——已修 / 误报 / 有意设计 / 不值得修。用户回复后把裁决追加一行到 `.scratch/review-feedback.md`（REVIEW.md 可指定其他路径）；这个 ledger 是修订 REVIEW.md 的依据，勿删。

## 6. 复审与防循环

复审只针对上次报告之后的新增改动。三条铁规（防 agent 作者与 agent reviewer 循环互审）：

- 只看上次报告之后的新增 diff，不重扫全量；
- 不重复上次已报告的 finding（除非新改动让它恶化）；
- 复审上限 2 轮，到顶输出终报并如实说明剩余风险。

## 7. 边界

- 审到报告为止：不自动修复、不自动 commit。
- subagent 不可用的环境：退化为单上下文自查，并在报告开头声明「未经交叉核实」。
