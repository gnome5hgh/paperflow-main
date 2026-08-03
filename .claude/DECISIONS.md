# DECISIONS.md — 为什么这样做（决策理由）

> **只追加，绝不改旧条目。** 每条记录：日期 + 决策 + 理由。新条目加在末尾。

## 2026-07-31 — 无确定性管线，一切由 LLM ReAct 驱动

**决策**：路由、工具选择、任务拆解全交给 LLM 的 ReAct 循环；Tool 只是喂给模型的 JSON Schema 定义。

**理由**：学术研究工作流意图多变，确定性管线会僵化；ReAct 循环天然适配开放领域任务。见 ADR 0003、CLAUDE.md「Key design decisions (Layer 0)」。

## 2026-07-31 — 权限最小化（三层规则）

**决策**：Supervisor 只拥有调度类 Tool（spawn/parallel/aggregate/ask_user），SubAgent 只拥有领域执行类 Tool；`allowed_agents` 限制特权 Skill，`allowed_spawns` 限制递归调度。

**理由**：即使 SubAgent 被注入恶意 prompt 也没有调度工具可用，安全边界最小化。见 ADR 0003。

## 2026-07-31 — 意图识别 = 框架服务，不是 SubAgent

**决策**：意图识别在 `Supervisor.run()` 的 ReAct 循环之前确定性执行，产出结构化 IntentOutput；Supervisor 的 Tool 列表不变。

**理由**：独立意图 SubAgent 每轮多一次 spawn LLM 调用，且需给 Agent 基类引入第二种形态；单用户本地场景下上下文隔离收益 < 成本。见 ADR 0007。

## 2026-07-31 — 意图契约落地偏离 ADR 0007 的 7-route 方案

**决策**：实际实现用 5-value 枚举（`SEARCH_PAPER` / `GENERATE_NOTE` / `ASK_QUESTION` / `MANAGE_MEMORY` / `GENERAL`），把 ADR 的 search/download 合并、read/answer/query_notes 合并；`READ_PAPER`/`QUERY_NOTES` 留到 Layer 4 细化时加入。

**理由**：契约层先定死最小实现集，避免「枚举允许但系统无处理路径」的悬空值；`download`/`mode` 等参数维度留到 Layer 4 扩 routes.yaml 时再细化。见 `intent_schema.py` 注释、commit `a549022`。

## 2026-07-31 — 记忆系统三种类型，Task Memory 被吸收

**决策**：Context（压缩摘要）/ User（MEMORY.md + Dream 后台）/ Experience（history.jsonl）；不保留独立 Task Memory 字典，也不保留独立 Memory Router。

**理由**：summary 的 `task_overview` + `current_state` + `next_steps` 已覆盖任务进度追踪；各记忆类型由生命周期自动驱动，无需额外路由组件。见 ADR 0004。

## 2026-07-31 — 失败传递：error_detail 只在相邻层可见

**决策**：spawn 返回结构化 `SubAgentResult(status, summary, error_detail, needs_attention)`；调用方能恢复就不上抛，不能恢复才做 condensed 摘要，error_detail 不跨级传递。

**理由**：上下文隔离——中间层错误不污染上层决策，同时保留可恢复的审计信息。见 ADR 0003。

## 2026-07-31 — ToolResult.summary 从第一天就存在

**决策**：`ToolResult.summary: dict` 默认空 dict，作为记忆系统的前向 hook。

**理由**：Layer 1+ 的记忆系统需要工具的结构化摘要，day-one 预留避免日后大规模改接口。见 CLAUDE.md。

## 2026-07-31 — GROBID 统一解析 PDF，回退 PyMuPDF

**决策**：所有 PDF（含 arXiv）统一走 GROBID（Docker，端口 8070）解析为结构化 section/表格/图注；GROBID 不可用时回退 PyMuPDF 启发式解析。

**理由**：解析质量优先统一入口；回退保证离线/服务挂掉时仍可读。见 CONTEXT.md。

## 2026-08-01 — 语义路由自研复现，而非 pip 安装 semantic-router

**决策**：HybridRouter 在 `core/intent/` 内自研复现 semantic-router 0.1.16 核心逻辑（融合检索、凸组合、阈值决策、fit/evaluate，含 `b*b` 公式 bug 决策 A 保留），不引入 semantic-router / litellm / bert tokenizer 依赖。

**理由**：semantic-router 引入 litellm/openai/tiktoken 等大依赖，而我们已有自研 LLMClient 与 StructuredOutput；jieba 替换 bert tokenizer（中英文意图）、DenseEncoder 接口替换 bge（真实 bge 留 RAG 栈统一装）。对照脚本 `scripts/compare_router.py` 用固定向量隔离分词差异，验证路由逻辑与 0.1.16 一致。见 ADR 0007 实现修订。

## 2026-08-01 — JiebaTokenizer vocab 冻结

**决策**：`JiebaTokenizer.build_vocab` 首次构建后冻结（token_id 语义稳定，新词 OOV 归 0），后续 fit 只重算统计量。

**理由**：jieba 动态构建 vocab 与 bert 固定 vocab（30522）不同——若每次 fit 重建，多次 add routes 后 token_id 漂移（旧索引 {3:"论文"} vs 新向量 {3:"下载"}），稀疏索引数据作废。语义稳定优先，新词丢失贡献可接受。见 `bm25_encoder.py`。

## 2026-08-01 — BM25 的 `b*b` 公式保留（决策 A）

**决策**：`encode_documents` 严格保留 semantic-router 0.1.16 的 `k1 * (1 - b*b * (len/avgdl))` 公式（源码笔误，docstring 写 `1-b+...` 但代码是 `b*b`）。

**理由**：对照验证的前提是与 0.1.16 行为完全一致——"修 bug"会让 ours/theirs accuracy 不可比。与 0.1.16 行为一致优先于"正确性"。

## 2026-08-01 — FixedDenseEncoder 确定性种子用 md5 而非内置 hash

**决策**：`_deterministic_seed` 用 `hashlib.md5(text).hexdigest()[:8]`，不用内置 `hash(text)`。

**理由**：内置 hash 受 PYTHONHASHSEED 随机化——同一文本跨进程（对照验证的 ours/theirs 两个独立进程）向量不同，对照直接失效。md5 保证跨进程确定性。

## 2026-08-01 — 意图知识库 routes.yaml 从第一天起

**决策**：意图知识库用 `data/intents/routes.yaml`（测试与生产共用 `load_routes()` 加载路径），最小验证集 4 类 × 2 条，完整 105 条标注待意图集合定稿后构建。

**理由**：避免"内存常量 → 文件"的迁移——测试用常量、生产用文件会导致数据路径不同，迁移时需验证等价。从一开始用文件，Layer 扩展时只换数据文件零代码改动。

## 2026-08-01 — 对照验证拆"写脚本"与"执行"

**决策**：`scripts/compare_router.py` 作为代码资产进实现（可审查、可复用）；隔离 venv 安装 semantic-router 0.1.16 + 执行对照留验收后手动。

**理由**：0.1.16 是 2023 库，在 Python 3.12 装 openai 0.28/pydantic 1.10 有兼容风险——implementer 陷入排错浪费 task 时间。脚本用 `--impl ours|theirs` 分支 + 延迟 import，两侧各跑一次对比 accuracy。

## 2026-08-01 — ADR 表述原则：正文 = 当前实现，不留修订注记

**决策**：ADR 文档直接描述当前实现的方案形态（用"已实现 / 待实现"标注状态），不写分层实施细节（"Layer N 怎么实现"、"留到 Layer N"），不留"实现修订/注记"节。

**理由**：ADR 是架构决策记录——旧设计 + 修订注记的双层结构让读者读两遍且易过时；直接同步正文为当前实现更可维护。实现揭示 ADR 设计缺陷时，融合进正文而非追加注记。
