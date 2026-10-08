# paperFlow

LLM 驱动的学术研究流程助手：一个交互式终端 REPL，围绕「读论文 → 做笔记 → 追问题」的科研工作流，提供多 Agent 协作、RAG 知识库检索、引用管理与记忆系统。

## 功能特性

- **多 Agent 协作**：supervisor 单根调度，读子 agent 能力清单（`<available_agents>`）按需拆解子任务并派发（searcher 文献搜索 / noter 论文笔记 / reviewer 审稿评阅 / qa-agent 问答 / researcher 深度研究 / librarian 文献库维护），各子 agent 返回结构化摘要后聚合回答
- **意图识别路由**：向量 + BM25 混合编码，多意图拆分为意图列表（顺序与并行由 supervisor 自主决定），低置信度时向用户澄清确认
- **RAG 检索栈**：GROBID 解析 PDF（不可达时回退 PyMuPDF 启发式分节）→ 学术分块（按节切 + 句界滑窗，丢弃参考文献/样板章节残渣）→ Milvus 向量 + BM25 混合检索 → RRF 融合 → 云端重排
- **引用管理**：BibTeX 文库读写、语料标题索引、引用溯源，支持 APA / GB/T 7714 等格式
- **视觉分析**：PDF 图表区域检测与提取，多模态模型看图解读
- **记忆系统**：对话持久化 + 分层记忆块 + 后台 sleeptime 记忆整合 + 会话回放（`--resume`）
- **Skills 机制**：`~/.paperflow/skills/` 单级目录 + 集中 lock 文件
- **MCP 接入**：桥接外部 MCP server 的工具（逐工具可见性、写类工具确认），预批准白名单

## 环境要求

- Python ≥ 3.11
- 一个 OpenAI 兼容的 LLM 端点（`config.yaml` 配置 base_url + api_key）
- 可选：Docker（Milvus Standalone + GROBID 服务栈；不启用时 RAG 稠密检索降级、PDF 解析走内置解析器）

## 快速开始

```bash
# 1. 安装（建议在虚拟环境 / conda env 中）
pip install -e .

# 2. 配置：复制模板并填入 LLM api_key
cp config.example.yaml config.yaml

# 3. （可选）启动依赖服务：Milvus 向量库 + GROBID PDF 解析
docker compose up -d

# 4. 启动交互式 REPL（首次启动会自动探测并拉起依赖服务）
paperflow
```

REPL 内置斜杠命令：`/help` 查看命令、`/skill` 管理 skills、`/version` 查看版本。历史会话可用 `paperflow --resume` 回放。

## 配置说明

所有可调参数的默认值唯一声明在 `paperflow/config.py`（dataclass 字段默认值），`config.yaml` 是纯覆盖文件——只写要改的值，未列出的字段回落代码默认值。主要配置段：

| 配置段 | 用途 |
|---|---|
| `llm` | 主 LLM：端点 / key / 模型 / 上下文窗口 |
| `vision` | 多模态视觉模型（图表分析，留空降级不可用） |
| `rag.embedding` | 云端嵌入 + 重排（留空则路由退纯 BM25、检索跳稠密路） |
| `rag.retriever` / `rag.chunker` / `rag.query_rewrite` | 检索参数 / 分块参数 / 查询改写 |
| `intent.encoder` / `intent.router` | 意图路由的编码器与阈值 |
| `corpus` | 笔记 / PDF / 研究产物的根目录路径 |
| `mcp_servers` | 外部 MCP server 接入（transport / 工具白名单 / 写类确认） |

API key 也可经环境变量提供（如 `PAPERFLOW_LLM_API_KEY`、`PAPERFLOW_RAG_EMBEDDING_API_KEY`）。

## 架构概览

```
paperflow/
  terminal/      终端交互层:REPL + 活动流渲染 + 确认中心 + 会话回放
  core/          核心运行层:agent(ReAct 循环) / intent(意图识别)
                 / memory(记忆系统) / llm / security(安全中间件)
                 / mcp(MCP 客户端平台) / structured(结构化输出)
  rag/           RAG 检索栈:解析 → 分块 → 向量存储 → 混合检索
  citations/     引用管理:bib 读写 + 语料索引 + 编排
  vision/        视觉分析:图表检测 + 提取 + 多模态解读
  agents/        各角色 agent 的系统提示词与工具装配
  tools/         面向 agent 的工具集:文件 / 记忆 / 引用 / RAG / 搜索等
```

数据流：用户输入 → 意图识别 → supervisor ReAct 循环（必要时 spawn 子 agent）→ 工具调用（本地工具 + MCP 工具）→ 结构化摘要聚合 → 流式回答。

## AI 协作入口

仓库根的 `AGENTS.md` 是面向所有 AI 编码代理（ZCode / Claude Code / Codex 等）的治理 manifest（架构、命令、编码规范、文档同步规则），并与子目录逐级 manifest（`paperflow/` 及其 `core/rag/tools/citations/vision/terminal/`、`agents/`、`.paperflow/`）构成两级路由，就近优先。`CLAUDE.md` 是 Claude Code 的入口薄指针（本地维护、不入库）。事实优先级：源码与测试 > AGENTS.md > `docs/` 设计文档。