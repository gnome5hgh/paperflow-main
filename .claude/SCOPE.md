# SCOPE.md — 项目做什么、不做什么

> Memory Bank 的一部分。按需更新；SessionStart 时与 HANDOFF.md 一起注入上下文。

## 做什么

**paperFlow**：基于 **Supervisor + SubAgent + Skills 插件** 三层架构的 LLM 驱动学术研究工作流助手。

- 单一入口：`python -m paperflow` 交互式 Supervisor REPL，自然语言驱动（无子命令模式）
- 框架层**意图识别**（服务，非 SubAgent）把用户输入解析为结构化意图 → Supervisor 拆解分发（当前 5 值意图集合：search_paper / generate_note / ask_question / manage_memory / general）
- 数据源：Obsidian Vault（`/Users/gnomeshgh/Documents/Obsidian Vault/paper/`）的 `note/` 与 `pdf/`
- RAG 检索：BM25 + Vector → RRF → reranking（bge-reranker-v2-m3），GROBID 解析 PDF
- 记忆系统：Context Memory（压缩摘要）/ User Memory（MEMORY.md 索引 + Dream 后台）/ Experience Memory（history.jsonl）
- 安全：Audit → WorkspacePolicy → SecurityScan → PolicyEngine 中间件管道（+ ExperienceMemory 第 ⑤ 个）
- 路由实现：HybridRouter 自研复现 semantic-router 0.1.16（`core/intent/`，保留 b*b 公式），jieba BM25 + DenseEncoder 接口（真实 bge 由 RAG 栈提供）

## 不做什么

- **不提供确定性管线**——路由、工具选择、任务拆解全由 LLM 的 ReAct 循环驱动
- **不索引 vault 以外的数据源**（RAG 仅索引 `note/` + `pdf/`）
- **Paper 不可被系统修改**（外部学术出版物；只有 Note 是系统产出的）
- **不实现 workspace 用户自定义 Agent 的覆盖逻辑**（Layer 0 只扫目录，同名覆盖留待有需求）
- **意图识别不是 SubAgent**——是框架服务，不参与 spawn，Supervisor 的 Tool 列表不变（仍仅 4 个调度类）
- **CLI 无子命令**——所有操作通过自然语言触发，不记忆命令格式
- **记忆系统不含 Knowledge Memory**——知识走独立的 RagRetrieveTool
- **无独立 Memory Router 组件**——各记忆类型由各自生命周期自动驱动
- **不引入 semantic-router / litellm / bert tokenizer 依赖**——路由自研复现，真实 bge 统一由 RAG 栈提供（避免提前 2GB 依赖）
- **对照验证 theirs 侧执行不纳入常规实现**——`scripts/compare_router.py` 是代码资产，隔离 venv 安装 0.1.16 手动执行

## 边界

- 单用户本地场景；DeepSeek / OpenAI 兼容 API 通过 `base_url` 切换
- 权限最小化：Supervisor 无执行类 Tool，SubAgent 无调度类 Tool
- ADR 表述原则：正文 = 当前实现形态，用"已实现/待实现"标注，不写分层实施细节或修订注记
