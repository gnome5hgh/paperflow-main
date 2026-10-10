# paperFlow

LLM 驱动的学术研究流程助手：一个交互式终端 REPL，围绕「读论文 → 做笔记 → 追问题」的科研工作流，提供多 Agent 协作、RAG 知识库检索、引用管理与记忆系统。

## 功能特性

- **多 Agent 协作**：supervisor 单根调度，读子 agent 能力清单（`<available_agents>`）按需拆解子任务并派发（paper-agent 文献检索与阅读 / review-agent 审稿评阅 / note-agent 论文笔记 / research-agent 选题与研究计划 / citation-agent 引用库维护 / rag-agent 语料索引与检索 / memory-agent 记忆与清单），各子 agent 返回结构化摘要后聚合回答
- **意图识别（可选预处理层，默认关）**：规则层高精度模式命中即定类，不命中交给结合对话史的判定服务，产出一条单类别信号注入根 agent；它只作信号，不决定派发顺序
- **RAG 检索栈**：本地版面解析 PDF（PyMuPDF 还原章节与坐标，不依赖外部服务）→ 学术分块（按节切 + 句界滑窗，丢弃参考文献/样板章节残渣）→ Milvus 向量 + BM25 混合检索 → RRF 融合 → 云端重排
- **引用管理**：BibTeX 文库读写、语料标题索引、引用溯源，支持 APA / GB/T 7714 等格式
- **视觉分析**：PDF 图表区域检测与提取，原图入 MinIO 对象存储，多模态模型看图解读
- **记忆系统**：对话持久化 + 分层记忆块 + 每轮收尾的记忆整合 + 会话回放（`--resume`）
- **Skills 机制**：`.paperflow/skills/` 单级目录 + 集中 lock 文件
- **MCP 接入**：桥接外部 MCP server 的工具，写类工具逐次确认，`write_tools` 预批准

## 环境要求

| 依赖 | 说明 |
|---|---|
| Python ≥ 3.11 | 运行环境，建议用虚拟环境（venv / conda）隔离 |
| Docker + Docker Compose | 依赖服务栈：Milvus Standalone 向量库 + etcd + MinIO 对象存储，由仓库根的 `docker-compose.yml` 一次拉起。向量检索与图表原图存储都跑在这套服务上 |
| uv | `uvx` 启动 MCP `paper-search` server（文献搜索与下载），配置见 `config.yaml` 的 `mcp_servers.paper-search` |
| LLM 端点 | 一个 OpenAI 兼容且支持工具调用的对话端点（base_url + api_key）。图表看图复用同一端点 |
| 嵌入 / 精排端点 | 一个 OpenAI 兼容的嵌入与重排端点（默认硅基流动 `Qwen/Qwen3-Embedding-0.6B` + `Qwen/Qwen3-Reranker-0.6B`）。RAG 稠密检索与重排都用它 |

## 快速开始

```bash
# 0. 装 uv（MCP paper-search 经 uvx 启动）
#    见 https://docs.astral.sh/uv/

# 1. 安装（建议在虚拟环境 / conda env 中）
pip install -e ".[dev]"

# 2. 配置：复制模板并填入 LLM 与嵌入/精排的 api_key
cp config.example.yaml config.yaml

# 3. 启动依赖服务（Milvus + etcd + MinIO）并等健康
docker compose up -d
curl http://localhost:9091/healthz   # → OK

# 4. 启动交互式 REPL（启动预检会自动探测依赖服务）
paperflow
```

REPL 内置斜杠命令：`/help` 查看命令、`/skill` 管理 skills、`/mcp` 查看 MCP server 状态、`/version` 查看版本。历史会话可用 `paperflow --resume` 回放。

### 一键安装提示词

把下面整段提示词复制给你的 AI 编码工具（ZCode / Claude Code / Codex 等），它会照着在当前仓库里把环境装好并验证：

```text
请帮我在本机把 paperFlow 的开发环境装好并验证通过。仓库就是当前工作目录。

要求：只做环境安装与配置，不要改任何源码。

步骤：
1. 先读仓库根的 AGENTS.md，按其中的 Commands 与环境要求执行。
2. 装 uv（https://docs.astral.sh/uv/）——MCP 的 paper-search server 经 uvx 启动。
3. 创建并激活一个 Python ≥ 3.11 的虚拟环境，然后安装：pip install -e ".[dev]"
4. 复制配置模板：cp config.example.yaml config.yaml
5. 向我要下面两个 API key 并写进 config.yaml，拿到之前不要填假值、不要跳过：
   - llm.api_key：OpenAI 兼容的对话端点 key（默认端点 https://api.deepseek.com/v1）
   - rag.embedding.api_key：OpenAI 兼容的嵌入/精排端点 key（默认 https://api.siliconflow.cn/v1）
6. 启动依赖服务栈并等它真的健康：
   docker compose up -d
   然后轮询 curl http://localhost:9091/healthz 直到返回 OK（首次会拉镜像，可能几分钟）。
7. 问我的论文 PDF 目录与笔记目录，填进 config.yaml 的 corpus.pdf_dir 与 corpus.note_dir。
   这两个是必填项；我若给不出，就把情况告诉我，不要随便填。
8. 验证：
   - python -m pytest tests/ -q（单测用 Milvus Lite 内嵌，不依赖服务栈）
   - 启动 paperflow，确认没有「依赖服务未就绪」这类警告
9. 每一步做完向我汇报。任何一步失败，把原始报错贴给我并说明你的修复方案，不要静默跳过或降级处理。
```

## 配置说明

所有可调参数的默认值唯一声明在 `paperflow/config/sections.py`（dataclass 字段默认值），`config.yaml` 是纯覆盖文件——只写要改的值，未列出的字段回落代码默认值。主要配置段：

| 配置段 | 用途 |
|---|---|
| `llm` | 主 LLM：端点 / key / 模型 / 上下文窗口 |
| `vision` | 多模态视觉模型（图表看图分析） |
| `rag.embedding` | 云端嵌入 + 重排端点与模型 |
| `rag.storage` / `rag.store_images` | Milvus 连接与图表原图的对象存储（MinIO） |
| `rag.retriever` / `rag.chunker` / `rag.query_rewrite` | 检索参数 / 分块参数 / 查询改写 |
| `intent` | 意图识别子层（默认关；开启需 Vercel AI Gateway 的网关 key） |
| `corpus` | 笔记 / PDF / 研究产物的根目录路径 |
| `agents.timeouts` | 子 agent 超时覆盖表（仅 YAML 可配） |
| `mcp_servers` | 外部 MCP server 接入（transport / 工具白名单 / 写类确认） |

API key 也可经环境变量提供（如 `PAPERFLOW_LLM_API_KEY`、`PAPERFLOW_RAG_EMBEDDING_API_KEY`、`PAPERFLOW_VISION_API_KEY`）。

## 架构概览

```
paperflow/
  terminal/      终端交互层:REPL + 活动流渲染 + 确认中心 + 会话回放
  core/          核心运行层:agent(ReAct 循环) / llm(客户端 + 结构化输出)
                 / intent(意图识别) / memory(记忆系统) / security(安全中间件)
                 / mcp(MCP 客户端平台) / skills(Skill 注册表) / tool(Tool 抽象)
  rag/           RAG 检索栈:解析 → 分块 → 向量存储 → 混合检索
  citations/     引用管理:bib 读写 + 语料索引 + 编排
  vision/        视觉分析:图表检测 + 提取 + 多模态解读
  tools/         面向 agent 的工具集:文件 / 记忆 / 引用 / RAG / 搜索等

agents/          各角色 agent 的 AGENT.md + tools.py（插件目录，位于仓库根）
.paperflow/      运行时数据根与资产目录:skills(流程 skill) / intent(意图知识库)
```

数据流：用户输入 →（可选）意图识别 → supervisor ReAct 循环（必要时 spawn 子 agent）→ 工具调用（本地工具 + MCP 工具）→ 结构化摘要聚合 → 流式回答。

## AI 协作入口

仓库根的 `AGENTS.md` 是面向所有 AI 编码代理（ZCode / Claude Code / Codex 等）的治理 manifest（架构、命令、编码规范、文档同步规则），并与子目录逐级 manifest（`paperflow/` 及其 `core/rag/tools/citations/vision/terminal/`、`agents/`、`.paperflow/`）构成两级路由，就近优先。`CLAUDE.md` 是 Claude Code 的入口薄指针（本地维护、不入库）。事实优先级：源码与测试 > AGENTS.md > `docs/` 设计文档。
