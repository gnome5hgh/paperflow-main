# Memory Bank — 跨会话项目记忆系统

> 面向第一次接触这套系统的开发者。读完你会明白：**它解决什么问题、每份文档什么时候被写入/读取、以及怎么把它装进你自己的项目。**
>
> 适用工具：Claude Code（claude.ai/code）及其 Hook 机制。其余场景（Cursor / Codex / 其他 agent）思路可迁移，但本文的 hook 实现是 Claude Code 专用的。

---

## 目录

- [0. 它解决什么问题](#0-它解决什么问题)
- [1. 核心概念（先读）](#1-核心概念先读)
- [2. 整体设计：一次会话的生命周期](#2-整体设计一次会话的生命周期)
- [3. 文件清单与职责](#3-文件清单与职责)
- [4. 每份文档的写入/读取时机（核心表）](#4-每份文档的写入读取时机核心表)
- [5. 三个 Hook 的详细行为](#5-三个-hook-的详细行为)
- [6. 「维护指令」：让 Claude 会话中主动维护交接](#6-维护指令让-claude-会话中主动维护交接)
- [7. 会话结束时的语义总结（claude --print）](#7-会话结束时的语义总结claude---print)
- [8. 如何部署到另一个项目](#8-如何部署到另一个项目)
- [9. 部署后验证清单](#9-部署后验证清单)
- [10. 设计取舍、已知限制与 FAQ](#10-设计取舍已知限制与-faq)

---

## 0. 它解决什么问题

用 Claude Code 开发大型项目时，有三个反复出现的痛点：

| 痛点 | 场景 |
|---|---|
| **① 上下文装不下** | 一个窗口干到一半，对话上下文满了。Claude Code 会把历史**压缩**成一段摘要，摘要会丢细节。 |
| **② 切窗口后失忆** | 于是你开新窗口。新窗口的 Claude 对项目一无所知，需要重新探索：项目结构？改到哪了？下一步做什么？有什么坑？ |
| **③ 重复劳动** | 每次切窗口，LLM 都重新读一遍代码、重新建立上下文，费时费钱。 |

**Memory Bank 的思路**：在窗口的**开始 / 压缩前 / 结束时**，自动读写几个小文档，把「项目做什么、干到哪、下一步、踩过什么坑」落盘。这样：

- 新窗口一打开，Claude **自动读到**上次留下的交接，不需要重新探索；
- 上下文压缩后，Claude 仍然记得**压缩瞬间**的状态，而不是会话开始时的状态；
- 交接的质量不依赖你碰巧说了什么——Claude 在干活过程中就持续维护它。

实现这套自动化靠的是 Claude Code 的 **Hook（钩子）**，不需要改 Claude Code 本身，也不需要装额外服务。

---

## 1. 核心概念（先读）

### 1.1 Hook（钩子）

Hook 是 Claude Code 内置的机制：在特定事件发生时，自动执行你配置的**一段 shell 命令**。Memory Bank 用了三个：

| 事件 | 触发时机 | 我们配置的命令 |
|---|---|---|
| `SessionStart` | 会话**开始**时（新窗口 / resume / clear / **compact 完成后**） | 运行 `memory_bank.sh start` |
| `PreCompact` | 上下文**即将被压缩**时 | 运行 `memory_bank.sh archive` |
| `SessionEnd` | 会话**结束时** | 运行 `memory_bank.sh end` |

Hook 的配置放在 `.claude/settings.json`（下面会讲）。

### 1.2 注入（additionalContext）

`SessionStart` hook 有一个特殊能力：它可以从 stdout **输出一段 JSON**，其中的 `hookSpecificOutput.additionalContext` 字段会被**注入进会话上下文**（相当于 Claude 开局就读到这段话）。Memory Bank 的核心就是靠这个把交接文档送进新窗口。

> ⚠️ 注意：hook 的**纯文本 stdout 会被忽略**，必须按 JSON 格式输出才会被注入。脚本里已经处理好了，你不需要关心格式，但要知道「注入」这个动作是这么来的。

### 1.3 压缩（Compaction）与「compact 后重触发」

Claude Code 在上下文快满时会**压缩**对话历史。关键机制：`SessionStart` hook 不仅在新窗口触发，**在压缩完成之后也会重新触发**（命令行里表现为 `source=compact`）。Memory Bank 利用这一点：压缩后重新注入交接文档，让 Claude 在压缩后的新上下文里仍然「知道自己在哪」。

### 1.4 易变 vs 稳定：文档分层

Memory Bank 把信息分成两类，区别对待：

| 类别 | 文档 | 处理方式 |
|---|---|---|
| **易变**（每个会话都可能变） | `HANDOFF.md`、`handoffs/` 快照 | 每次会话重写 / 覆盖，**不进 git** |
| **稳定**（少变、长期有效） | `SCOPE.md`、`SYSTEM.md`、`DECISIONS.md` | 人工维护，**进 git**，随项目走 |
| **配置** | `settings.json`、`settings.local.json` | 前者进 git，后者是个人本地配置不进 git |

---

## 2. 整体设计：一次会话的生命周期

```
┌─ 窗口 A ─────────────────────────────────────────────────────────────┐
│                                                                        │
│  [SessionStart hook]  start                                           │
│      └─ 读取 HANDOFF.md + SCOPE.md（+ 压缩快照）→ 注入到会话上下文        │
│      └─ 顺便告诉 Claude「维护指令」（见 §6）                             │
│                                                                        │
│   Claude 开始干活：                                                      │
│      · 每完成一个里程碑 → 主动更新 HANDOFF.md 的「下一步做什么」             │
│      · 上下文快满 / 要切窗口 → 主动把当前状态写进「上次在哪」                │
│                                                                        │
│  [上下文即将压缩]                                                       │
│  [PreCompact hook]  archive                                            │
│      └─ 备份 HANDOFF.md → handoffs/HANDOFF-<时间戳>.md                  │
│      └─ 从会话 transcript 提取尾部 → 写 handoffs/latest-snapshot.md      │
│  → 压缩完成 → [SessionStart hook 再次触发, source=compact] start          │
│      └─ 重新注入 HANDOFF + SCOPE + 「压缩前快照」                         │
│         （Claude 继续干活，且记得压缩瞬间的状态）                          │
│                                                                        │
│  [会话结束]                                                            │
│  [SessionEnd hook]  end                                                │
│      └─ 先备份旧 HANDOFF → handoffs/                                    │
│      └─ 从 transcript 提取会话尾部（可选交给 claude --print 总结成语义）    │
│      └─ 附加 git 事实（分支 / HEAD / 未提交数 / 最近提交）                │
│      └─ 按模板重写 HANDOFF.md（「下一步」留待 Claude 维护）                │
└────────────────────────────────────────────────────────────────────────┘

┌─ 窗口 B（新窗口）────────────────────────────────────────────────────┐
│  [SessionStart hook] start                                           │
│      └─ 直接读到窗口 A 留下的 HANDOFF.md → 无缝续接，不用重新探索项目      │
└────────────────────────────────────────────────────────────────────────┘
```

一句话总结：**SessionStart 负责「喂」，SessionEnd 负责「收」，PreCompact 负责「压缩瞬间抓拍」**，Claude 在会话中负责「保持新鲜」。

---

## 3. 文件清单与职责

Memory Bank 的完整文件布局（以项目根目录的 `.claude/` 为家）：

```
项目根/.claude/
├── settings.json             # hooks 配置（进 git）——告诉 Claude Code 何时跑脚本
├── settings.local.json       # 个人权限配置（不进 git）——只属于你，如允许执行的命令白名单
├── HANDOFF.md                # 会话交接（不进 git，每次会话重写）
├── SCOPE.md                  # 项目做什么 / 不做什么（进 git，每次会话自动注入）
├── SYSTEM.md                 # 系统怎么运作 / 约束 / 踩过的坑（进 git，按需读取）
├── DECISIONS.md              # 决策理由，只追加（进 git，按需读取）
├── MEMORY_BANK.md            # 本文档（进 git）
├── scripts/
│   └── memory_bank.sh        # 核心脚本（进 git）——三个 hook 都调它
└── handoffs/                 # 存档目录（不进 git）
    ├── latest-snapshot.md    # 压缩快照：压缩前抓拍当前状态（每次覆盖，只留最新）
    └── HANDOFF-<时间戳>.md   # HANDOFF 历史备份（PreCompact / SessionEnd 各写一份）
```

各文件一句话职责：

| 文件 | 一句话职责 |
|---|---|
| `settings.json` | 声明三个 hook → 让 Claude Code 在开始/压缩前/结束时调用脚本 |
| `settings.local.json` | 你的个人权限白名单（与 hooks 无关，但二者共存于 `.claude/`） |
| `HANDOFF.md` | **当前工作状态**：上次在哪、git 事实、下一步做什么。新窗口的「记忆」 |
| `SCOPE.md` | 项目边界：做什么 / 不做什么。防止 Claude 越界或做错方向 |
| `SYSTEM.md` | 架构、关键机制、操作约束、踩坑。Claude 遇到架构/环境问题时按需读 |
| `DECISIONS.md` | 为什么这么设计。Claude 想改架构前先看这里，避免推翻既定决策 |
| `memory_bank.sh` | 所有读写逻辑的载体：注入、备份、快照、重写 HANDOFF |
| `latest-snapshot.md` | 压缩瞬间的状态抓拍，压缩后重新注入用 |
| `handoffs/HANDOFF-*.md` | 历史备份，防误覆盖，可回溯 |

---

## 4. 每份文档的写入/读取时机（核心表）

> 这是理解整个系统的关键。**谁写、谁读、什么时候写、什么时候读**，全在这里。

### 4.1 总表

| 文件 | 谁写入 | 写入时机 | 谁读取 | 读取时机 | 进 git |
|---|---|---|---|---|---|
| `HANDOFF.md` | ① `SessionEnd` hook<br>② Claude（按维护指令） | ① 会话结束时**重写**（尾部 + git 事实 + 占位「下一步」）<br>② 会话中：完成里程碑 / 上下文快满 / 要切窗口时**更新** | ① `SessionStart` hook<br>② `PreCompact` hook<br>③ Claude | ① 会话开始 & 压缩后，**注入**给新上下文<br>② 压缩前，**读取→备份**<br>③ 会话中按维护指令读/写 | ❌ 否 |
| `SCOPE.md` | 人工（手动编辑） | 项目边界变化时（少变） | `SessionStart` hook | 每次会话开始 & 压缩后，**注入** | ✅ 是 |
| `SYSTEM.md` | 人工（手动编辑） | 发现新机制 / 踩到新坑时（少变） | Claude | **按需**读（不自动注入，注入文本里有提示行引导） | ✅ 是 |
| `DECISIONS.md` | 人工（手动编辑，**只追加**） | 做出架构决策时 | Claude | **按需**读 | ✅ 是 |
| `settings.json` | 人工（部署时一次） | 部署 / 改 hook 命令时 | Claude Code | **进程启动**时加载（定义 hook 行为） | ✅ 是 |
| `settings.local.json` | 人工 + Claude Code 自动 | 需要授权/改权限时 | Claude Code | 进程启动时与 `settings.json` **合并** | ❌ 否 |
| `scripts/memory_bank.sh` | 人工（部署/升级） | 系统升级时 | 三个 hook | 事件触发时**调用执行** | ✅ 是 |
| `handoffs/latest-snapshot.md` | `PreCompact` hook | 压缩前，**整体覆盖**（只留最新一份） | `SessionStart` hook | **仅** `source=compact` 且该快照比 HANDOFF 新时，**注入** | ❌ 否 |
| `handoffs/HANDOFF-<时间戳>.md` | `PreCompact` + `SessionEnd` hook | 压缩前、会话结束覆盖前，各写一份**备份** | 人工 | 需要回溯旧交接 / 排障时 | ❌ 否 |

### 4.2 按「时机」视角再看一遍（等价但更直白）

**会话开始（SessionStart）：**
- **读**：`HANDOFF.md`、`SCOPE.md`（必读，注入）；`latest-snapshot.md`（仅压缩后且更新时）
- **写**：无（只读不写）

**会话中（Claude 按维护指令）：**
- **读**：`HANDOFF.md`
- **写**：`HANDOFF.md` 的「上次在哪」「下一步做什么」段

**压缩前（PreCompact）：**
- **读**：`HANDOFF.md`（为备份）、会话 transcript（为抓尾部）
- **写**：`handoffs/HANDOFF-<时间戳>.md`（备份）、`latest-snapshot.md`（快照）

**会话结束（SessionEnd）：**
- **读**：会话 transcript（提取尾部）、git（分支/HEAD/未提交/最近提交）
- **写**：`handoffs/HANDOFF-<时间戳>.md`（先备份旧 HANDOFF）、`HANDOFF.md`（重写）、删除 `latest-snapshot.md`（已过期）

### 4.3 一句话记忆口诀

> **开头喂、压缩抓拍、结尾收。HANDOFF 每次重写、快照只留最新、备份只增不减、DECISIONS 只追加。**

---

## 5. 三个 Hook 的详细行为

### 5.1 SessionStart → `memory_bank.sh start`

输入：stdin 是一段 JSON，含 `source` 字段（`startup` / `resume` / `clear` / `compact`）。

输出：JSON 注入文本，按顺序包含：

1. **维护指令**（固定文本，见 §6）
2. **`## HANDOFF.md`** —— 当前 HANDOFF 内容
3. **（可选）`## 压缩前快照`** —— 仅当 `source == compact` **且** 快照的修改时间比 HANDOFF 新（「prefer 更新者」：Claude 主动维护的语义内容优先于原始尾部快照）
4. **`## SCOPE.md`** —— 项目边界
5. **按需读取提示行** —— 提醒 Claude 遇到架构问题读 `SYSTEM.md`、遇到「为什么这么设计」读 `DECISIONS.md`

### 5.2 PreCompact → `memory_bank.sh archive`

输入：stdin JSON，含 `transcript_path`（当前会话记录文件的路径）。

操作（两步）：
1. **备份**：把当前 `HANDOFF.md` 复制为 `handoffs/HANDOFF-<时间戳>.md`
2. **抓拍**：从 transcript 尾部提取最近一段 user/assistant 文本，整体覆盖写入 `latest-snapshot.md`

`HANDOFF.md` 本体**不动**——它是 Claude 语义维护的，原始尾部只作为兜底进「压缩后上下文」，不污染交接文件。

### 5.3 SessionEnd → `memory_bank.sh end`

输入：stdin JSON，含 `session_id` / `transcript_path` / `reason`。

操作（五步）：
1. **先备份再覆盖**：把当前 `HANDOFF.md` 复制到 `handoffs/`（防误覆盖，任何情况都有留档）
2. **提取尾部**：从 transcript 提取最近 ~3KB 文本；**若为空则直接退出、不覆盖**（防止用垃圾覆盖好内容）
3. **语义总结（可选）**：若 `claude` CLI 在 PATH 上，用 `claude --print` 把尾部总结为语义交接（标注「语义总结（claude --print）」）；失败/不可用/空输出时静默回退原始尾部（见 §7）
4. **附加 git 事实**：分支 / HEAD / 未提交文件数 / 最近 5 条提交（best-effort，git 失败不影响 hook 退出）
5. **按模板重写 `HANDOFF.md`**，并删除已过期的 `latest-snapshot.md`

重写后的 HANDOFF 模板：

```markdown
# HANDOFF.md

自动生成：2026-08-03 10:30:32；session=xxx；reason=prompt_input_exit

## 上次在哪（会话尾部摘录 / 语义总结（claude --print））
<Claude 在上次会话干了什么、进展到哪>

## 当前状态快照（git 事实，自动附加，勿改）
- 分支：main
- HEAD：63b8784
- 未提交改动：2 个文件
- 最近 5 条提交：
  63b8784 fix: ...
  e3459bc feat: ...

## 下一步做什么
<Claude 主动维护：目标 / 当前层与任务 / 改动文件 / 待决问题 / 下一步 / 风险>
```

---

## 6. 「维护指令」：让 Claude 会话中主动维护交接

**痛点**：如果只靠 SessionEnd 从 transcript 抓尾巴，交接质量取决于你碰巧说了什么。真正的语义交接应该由「拥有完整上下文的 Claude 本尊」在干活过程中写。

**做法**：SessionStart 注入的文本里固定带一段「维护指令」，相当于给 Claude 的会话内规则：

> **维护指令（本会话必须遵守）**
> 1. 每完成一个里程碑/任务，更新 `.claude/HANDOFF.md` 的「下一步做什么」段（简洁、带优先级）
> 2. 感知到上下文接近满、将被压缩或要切窗口时，先把当前状态写入「上次在哪」段
> 3. 字段约定：目标 / 当前层与任务 / 改动文件 / 待决问题 / 下一步 / 风险
> 4. 不要删除或改写「自动生成」行与「当前状态快照」段（由 hook 管理）

这样 HANDOFF 在会话中途就是新鲜的：压缩时 PreCompact 抓拍到的是最新状态，新窗口读到的是上一会话的完整进展。

---

## 7. 会话结束时的语义总结（claude --print）

默认开启。SessionEnd 时，若 `claude` CLI 可用，脚本把 transcript 尾部交给 `claude --print`：

```
将以下 Claude Code 会话尾部摘录总结为 HANDOFF 的「上次在哪」段。
只输出总结本身，中文，覆盖：当前目标、进展到哪、阻塞/疑问、下一步。150 字以内。
```

产出会标注为「语义总结（claude --print）」。

- **价值**：把原始对话碎片整理成结构化交接；
- **成本**：每个会话结束多一次 LLM 调用；
- **降级**：`claude` 不可用、调用失败、输出为空 → 静默回退为原始尾部摘录，不影响 hook 退出；
- **递归防护**：`claude --print` 子进程若也触发 SessionEnd hook，会通过环境变量 `MEMORY_BANK_SESSION_END=1` 检测到并跳过摘要调用，防止无限递归。

想关闭：把脚本里 `if [[ -z "${MEMORY_BANK_SESSION_END:-}" ]] && command -v claude ...` 这一行改成只保留回退分支，或注释掉整个 `if` 块。

---

## 8. 如何部署到另一个项目

### 前提

- 装了 [Claude Code](https://code.claude.com)，且能在项目目录里正常启动；
- 机器上有 `bash`、`jq`、`git`（脚本依赖；`claude --print` 是可选的）。

### 步骤 1：拷贝文件

把整个 `.claude/` 目录（或下列文件）拷到新项目的 `.claude/` 下：

```
.claude/
├── settings.json           # hooks 配置（必拷）
├── SCOPE.md                # 必拷，然后改成你项目的边界
├── SYSTEM.md               # 必拷，然后改成你项目的架构/约束
├── DECISIONS.md            # 必拷，清空内容改成空模板
├── MEMORY_BANK.md          # 本文档（可选，作为说明）
├── scripts/
│   └── memory_bank.sh      # 核心脚本（必拷）
└── handoffs/               # 可拷可不拷（空目录，首次运行会自动创建）
```

`settings.local.json` **不要拷**——它是个人权限白名单，属于你而不是项目。

拷贝后确认脚本可执行：

```bash
chmod +x .claude/scripts/memory_bank.sh
```

### 步骤 2：写你的三份稳定文档

- **`SCOPE.md`**：写清「项目做什么 / 不做什么 / 边界」。这是 Claude 每次会话都会注入的，决定它不会跑偏。
- **`SYSTEM.md`**：写架构分层、关键机制、操作约束（比如「必须用 xx 命令跑测试」「不要改 docs/」）、踩过的坑。不用一次写完，边用边补。
- **`DECISIONS.md`**：空模板开头，约定「只追加」。每次做出架构决策就追加一条（日期 + 决策 + 理由）。

### 步骤 3（推荐但可选）：让静态资产进 git

Memory Bank 的价值之一是「换机器 / worktree / 重新 clone 后机制还在」，所以把静态资产提交进 git。在你的项目 `.gitignore` 里加入：

```gitignore
# Claude Code Memory Bank — 静态资产进 git，易变/个人文件保持忽略
# 注意：反选子文件必须用 .claude/* 排除内容而非 .claude/ 排除目录本身
.claude/*
!.claude/settings.json
!.claude/scripts/
!.claude/scripts/*
!.claude/SCOPE.md
!.claude/SYSTEM.md
!.claude/DECISIONS.md
!.claude/MEMORY_BANK.md
```

> ⚠️ **不要**写成 `.claude/`（带斜杠）再在后面 `!.claude/xxx` 反选——git 对「父目录被排除」的子文件反选无效。必须用 `.claude/*`。

然后提交：

```bash
git add .gitignore .claude/settings.json .claude/scripts/ .claude/SCOPE.md .claude/SYSTEM.md .claude/DECISIONS.md .claude/MEMORY_BANK.md
git commit -m "feat: 引入 Memory Bank 跨会话记忆系统"
```

`HANDOFF.md`、`handoffs/`、`settings.local.json` 保持忽略（它们是你和当前会话的，不该进 git）。

### 步骤 4：首次运行

打开一个新窗口（或 `/clear` 一次），Memory Bank 会自动：
- 创建 `.claude/HANDOFF.md` 和 `.claude/handoffs/`；
- 注入维护指令 + HANDOFF + SCOPE 到会话上下文。

### 部署到多个 worktree / 换机器

- 因为 `settings.json` + `scripts/` 进了 git，**任何 worktree checkout 都会自动带上 hooks**，Memory Bank 在 worktree 里直接生效；
- 每个 worktree 各自维护自己的 `HANDOFF.md`（符合分层开发隔离）；
- 换机器后 clone 仓库 → 静态资产 + hooks 都在 → 只需重新装 `jq`。

---

## 9. 部署后验证清单

按顺序确认这套系统真的在工作：

1. **脚本语法**：`bash -n .claude/scripts/memory_bank.sh` → 无输出即通过。
2. **start 能输出合法注入 JSON**：
   ```bash
   echo '{}' | bash .claude/scripts/memory_bank.sh start | jq .hookSpecificOutput.additionalContext
   ```
   应看到维护指令 + HANDOFF 段 + SCOPE 段。
3. **archive 能抓拍**：构造一个假 transcript：
   ```bash
   printf '{"type":"assistant","message":{"role":"assistant","content":"完成 X"}}\n' > /tmp/t.jsonl
   echo "{\"transcript_path\":\"/tmp/t.jsonl\"}" | bash .claude/scripts/memory_bank.sh archive
   cat .claude/handoffs/latest-snapshot.md   # 应含「完成 X」
   ```
4. **end 能重写 HANDOFF**：
   ```bash
   echo '{"session_id":"t","transcript_path":"/tmp/t.jsonl","reason":"prompt_input_exit"}' | bash .claude/scripts/memory_bank.sh end
   cat .claude/HANDOFF.md   # 应有「上次在哪」「当前状态快照」「下一步做什么」三段
   ```
5. **真实验证（最重要）**：
   - 开新窗口，看会话开头是否出现「Memory Bank 注入（SessionStart）」；
   - 干一会儿活，手动 `/compact`，看压缩后是否还记得刚才的状态、注入里有没有「压缩前快照」段；
   - 关掉窗口，看 `.claude/HANDOFF.md` 是否被更新、`.claude/handoffs/` 是否多了备份。

---

## 10. 设计取舍、已知限制与 FAQ

### 设计取舍

| 取舍 | 决定 | 理由 |
|---|---|---|
| 交接来源 | 优先「Claude 会话中主动维护」，SessionEnd 尾部/总结为兜底 | 前者零成本且上下文最新鲜；hook 是 bash，跑不了 LLM |
| 易变/稳定分层 | HANDOFF 每次重写；SCOPE/SYSTEM/DECISIONS 长期保留 | 体积控制 + 信息分类 |
| 压缩快照 | 只留最新一份，且「prefer 更新者」注入 | 防止快照膨胀；语义内容优先于原始尾部 |
| 静态资产进 git | 是（settings/scripts/稳定文档） | 换机器/worktree/clone 后机制仍生效 |
| `claude --print` 总结 | 默认开，可关 | 成本换质量，静默降级保底 |

### 已知限制

1. **依赖「SessionStart 在 compact 后重触发」**：这是压缩恢复链路（§5.2 + §1.3）的根基，按官方文档标注生效。若某版本不触发，快照注入会退化为纯备份，其余功能不受影响。
2. **并行窗口是「串行假设」**：设计假设你是一个窗口干完再开下一个。真·多窗口并行结束会「后写覆盖先写」。防线是「覆盖前先备份」——旧内容永远在 `handoffs/`，不会丢。
3. **`claude --print` 每次会话结束多一次 LLM 调用**：介意成本的用户可关闭（见 §7）。
4. **macOS 特有**：脚本用 BSD 语法 `stat -f %m` 比较修改时间，Linux 上需改成 `stat -c %Y`。macOS 自带的 bash 3.2 有「`$var` 后紧跟多字节字符会吞进变量名」的坑，脚本已用 `${}` 包裹规避，**请勿**在脚本里新写裸 `$var`。

### FAQ

**Q：HANDOFF 每次被重写，Claude 会话中维护的内容不就没了吗？**
A：没有丢——SessionEnd 覆盖前先把旧 HANDOFF 备份到 `handoffs/`。Claude 维护的「下一步」主要价值在**压缩恢复路径**：压缩前 PreCompact 抓拍到的就是维护后的最新状态。如果你希望维护内容直达下一个窗口，可在 end 时保留旧 HANDOFF 的非占位「下一步」段（当前未实现，是已知优化点）。

**Q：这个系统和 Claude Code 自带的记忆（claude-mem / 自动记忆）冲突吗？**
A：不冲突，但职责建议分开：claude-mem / 自动记忆负责「学到的知识、决策」，Memory Bank 负责「干到哪、下一步」。两者信息源不同（知识库 vs 工作状态）。

**Q：为什么要用 hook，不让 Claude 自己记得写？**
A：hook 是**确定性**的——无论 Claude 是否自觉，事件一到脚本就跑。Claude 主动维护是**锦上添花**（质量），hook 是**保底**（一定发生）。

**Q：能在不用 git 的项目里用吗？**
A：能。进 git 只是为了跨设备/跨 worktree 携带静态资产；`settings.json` + 脚本在本地一样工作。

**Q：我想改交接格式 / 注入内容？**
A：改 `.claude/scripts/memory_bank.sh` 里 `start` 的 `context` 拼装、`end` 的 heredoc 模板即可。改完跑一遍 §9 的验证清单。

---

> **附**：本系统本身的设计理由完整记录在 `.claude/DECISIONS.md` 的 `2026-08-03 — Memory Bank v2` 条目；实现细节以 `.claude/scripts/memory_bank.sh` 为准。
