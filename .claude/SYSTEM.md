# SYSTEM.md — 系统怎么运作、约束、踩过的坑

> Memory Bank 的一部分。按需读取（不自动注入，省上下文）。内容多为「不读代码就不容易知道」的事实。

## 分层与依赖

```
Layer 0  Core Framework（tool.py / agent.py / llm.py / agent_registry.py / config.py）✅
Layer 1  Framework Services：1.1 Security ✅ 1.2 Memory ✅ 1.3 Intent Recognition ✅（226 tests 全绿）
Layer 2  Tools & RAG（paperflow/tools/ 一工具一文件 + rag/）
Layer 3  Business Agents（search-paper / answer-question / generate-note / review-note）
Layer 4  Orchestration（Supervisor / CLI REPL / SubAgentResult 失败传播）
```

- Layer N 前置 Layer N-1。完整规格见 `docs/superpowers/specs/2026-07-31-full-layers-roadmap.md`
- 每层节奏：brainstorming → spec → plan → 实现 → 测试（已完成的 spec/plan 见 roadmap 索引表）

## 关键机制（代码级事实）

- **AgentRegistry 是唯一入口**：扫描 `agents/<name>/`，同时解析 `SKILL.md` frontmatter + 动态导入 `tools.py` 的 `TOOLS`
- **Agent.run() = 异步 ReAct 循环**（pull 模式，`get_config(agent_type)` 加载工具）；system 消息顺序固定：① SKILL.md → ② MEMORY.md 索引 → ③ 压缩摘要 → ④ user task；超过 `max_turns`（默认 20）抛 `MaxTurnsExceeded`
- **安全中间件固定顺序**（`Agent._exec_tool`）：before ① Audit → ② WorkspacePolicy → ③ SecurityScan → ④ PolicyEngine；after 逆序（⑤ ExperienceMemory 记经验）；`on_finish` 扫 LLM 最终回答。控制流异常驱动：`PolicyDenied` / `ConfirmRequired` / `SecurityBlocked`
- **记忆**：ExperienceMemory 是第 ⑤ 个中间件（只 after），工具调用自动写 history.jsonl；User Memory 每轮 run 开头重读 MEMORY.md 注入 system（Dream 间隙写入 → 下一轮生效）；ContextCompressor 每轮 llm.chat 前查 token 阈值（tiktoken 估算 ×1.1 buffer，三段重组保留头部 system）
- **意图识别管线**（`core/intent/`，自研复现 semantic-router 0.1.16）：Stage 0 实体提取（stub）→ Stage 1 追问检测（stub，依赖会话 prev_intent）→ Stage 2 HybridRouter（jieba BM25 稀疏 + DenseEncoder 稠密融合，alpha=0.3）→ Stage 3 StructuredOutput 兜底（IntentionResult + near_miss 注入）。见 ADR 0007
- **StructuredOutput**（core/structured.py，ADR 0006）：三层防御 = 生成约束 → pydantic 校验重试 → fallback；递归 Schema 展开；意图识别与上下文压缩复用

## 操作约束（易踩坑）

- **永远用 `conda run -n paperflow`**，不要裸 `python`/`pip`（依赖只在 conda env 里）
- 测试：`conda run -n paperflow python -m pytest tests/ -v`（当前 226 passed）
- 运行应用需要 `PAPERFLOW_API_KEY`（DeepSeek）
- **GROBID 是 Docker 服务**（`-p 8070:8070 lfoppiano/grobid:0.8.0`），未启动时回退 PyMuPDF 启发式解析
- **文档同步规则**：改代码必须同步更新关联 spec（`docs/superpowers/specs/`）与 plan（`docs/superpowers/plans/`）；上层 Layer 受影响需回修；ADR 一般不反向修改，但实现揭示 ADR 缺陷时在 ADR 追加修正说明
- **ADR 表述原则**：正文 = 当前实现形态，用「已实现 / 待实现」标注状态，不写分层实施细节、不留「实现修订/注记」节
- **代码注释必须中文，讲 WHY 不讲 WHAT**
- **jq**：Memory Bank 脚本（`.claude/scripts/memory_bank.sh`）依赖 jq
- **claude --print（可选兜底）**：SessionEnd 检测到 `claude` CLI 在 PATH 上时，会把会话尾部总结为语义交接（「上次在哪」标注「语义总结」）；不可用/失败/输出为空时静默回退为原始尾部摘录，不影响 hook 退出

## 踩过的坑

- **jieba 动态 vocab 需冻结**：BM25 tokenizer 的 vocab 首次 `build_vocab` 后必须冻结——否则多次 `add` routes 时 token_id 漂移（旧索引 {3:"论文"} vs 新向量 {3:"下载"}），稀疏索引数据作废。新词 OOV 归 0（`JiebaTokenizer.build_vocab` 的 `if vocab_size > 1: return`）
- **`b*b` 公式必须保留**：`BM25Encoder.encode_documents` 用 `k1 * (1 - b*b * (len/avgdl))`（semantic-router 0.1.16 源码笔误，决策 A 保留）——与 0.1.16 行为一致，对照验证依赖此保真
- **内置 `hash()` 跨进程不稳定**：`FixedDenseEncoder` 的种子必须用 md5（`_deterministic_seed`）——内置 hash 受 PYTHONHASHSEED 随机化，对照验证（ours/theirs 两个独立进程）会失效
- **HybridRouter.add 维度匹配**：fit 用全部累积 routes、编码入索引仅新增——若都用全部累积而 route 名只给新增，第二次 add 时 `np.concatenate` 长度不匹配崩溃
- **意图契约文件命名**：实际是 `paperflow/core/intent/schema.py` + `intent_schema.py`，在 `paperflow/core/` 下（不是顶层 `intent/`）
- **意图枚举是硬约束**：`枚举 = 契约 = 当前实现集`——不允许「枚举允许但系统无处理路径」的悬空值；新增意图要同步扩 routes.yaml（`load_routes` 校验 route 名 ∈ IntentType + utterances 非空）
- **`data/intents/` 需 .gitignore 例外**：`data/*` 默认忽略，加 `!data/intents/` 才能跟踪 routes.yaml
- **docs/ 整个目录 gitignored**：设计文档不进 git、无法跨设备同步；本 Memory Bank 在 `.claude/` 同样 gitignored，备份需自行处理
