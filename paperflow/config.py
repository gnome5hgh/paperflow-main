# paperflow/config.py
"""
全局配置模块，提供 LLM 连接参数和项目运行时配置。

配置加载优先级（从低到高）：
    1. dataclass 默认值（本文件是所有可调参数值的**唯一声明点**——改默认值只改这里）
    2. config.yaml（可选，文件不存在则跳过；纯覆盖文件，不重复声明默认值）
    3. 环境变量（最高优先级，按 ``PAPERFLOW_`` + 配置路径大写派生）

配置结构与 config.yaml 同构：
``runtime`` / ``corpus`` / ``intent`` / ``rag{embedding, rerank,
retriever, query_rewrite, chunker, storage, tools}`` / ``memory`` /
``session`` / ``agents{timeouts}``，外加保留的顶层 ``llm`` / ``vision`` / ``mcp_servers``。

env 名约定：字段路径以 ``_`` 连接并大写，前缀 ``PAPERFLOW_``。例如
``rag.storage.uri`` → ``PAPERFLOW_RAG_STORAGE_URI``，
``rag.query_rewrite.model`` → ``PAPERFLOW_RAG_QUERY_REWRITE_MODEL``。无例外表；
``agents.timeouts`` 与 ``mcp_servers`` 是自由 dict，仅 YAML 可配（不派生 env）。

使用方式::

    config = PaperFlowConfig.from_env()          # 自动加载
    config = PaperFlowConfig.from_env("my.yaml") # 指定 YAML 路径
"""

import os
import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


def _default_compaction():
    """CompactionSettings 惰性导入——compaction.py 依赖 llm.py、llm.py 依赖本模块，
    顶层 import 会构成 config→compaction→llm→config 循环（部分初始化导入失败）。
    字段默认值走此工厂，把导入推迟到首次构造时，此刻 config 已完整加载。"""
    from paperflow.core.memory.compaction import CompactionSettings
    return CompactionSettings()


@dataclass
class LLMConfig:
    """LLM 连接配置，封装 OpenAI-compatible API 所需的所有参数。

    默认值指向 DeepSeek API（通过 OpenAI SDK 兼容层调用），
    修改 base_url 可切换到任意兼容服务（如 OpenAI、vLLM、Ollama 等）。

    Attributes:
        base_url: str，API 基础地址（默认 DeepSeek 兼容端点）
        api_key: str，密钥（无硬编码默认值，留空由 LLMClient 报清晰错误）
        model: str，模型名
        max_tokens: int，单次响应输出上限（给足防长草稿被截断）
        temperature: float，采样温度（0.0 = 确定性，适合工具调用）
        timeout_connect: float，HTTP 连接超时（秒）
        timeout_read: float，HTTP 读超时（秒，含流式 chunk 间隔）
        max_retries: int，传输层自动重试次数（连接错误/5xx）
        context_window: int，模型上下文窗口（压缩预算来源）
    """

    #: API 基础地址，默认为 DeepSeek 兼容端点
    base_url: str = "https://api.deepseek.com/v1"

    #: API 密钥——**不硬编码默认值**。现必须经 PAPERFLOW_LLM_API_KEY env / .env /
    #: config.yaml llm.api_key 提供;留空由 LLMClient.__init__ 兜底报清晰错误。
    api_key: str = ""

    #: 模型名称，传给 API 的 model 参数
    model: str = "deepseek-v4-flash"

    #: 单次响应输出上限——deepseek-v4-flash 官方最大输出 384K（max_tokens 合法范围 1-393216）。
    #: 必须给足:上限过小会把长笔记草稿/大参数 write_file 静默截断成残缺内容。
    max_tokens: int = 393216

    #: 采样温度，0.0 表示确定性输出（适合工具调用场景）
    temperature: float = 0.0

    #: LLM HTTP 连接超时（秒）——连接挂死时秒级暴露而非无限等待
    timeout_connect: float = 10.0

    #: LLM HTTP 读超时（秒，含流式 chunk 间隔）——长输出（max_tokens 上限下单次
    #: 可跑数分钟）不能被误杀，取 300s；两个相邻 chunk 间隔超过此值即判定服务挂死
    timeout_read: float = 300.0

    #: 传输层自动重试次数（连接错误/5xx 时 SDK 原生重试，与上层业务重试无关）
    max_retries: int = 2

    #: 模型上下文窗口——deepseek-v4-flash 官方 1M。ContextCompressor.resolve_context_size
    #: 取半窗口 = 500K → 压缩阈值 400K、reserve 50K，正常对话永不压缩（1M 上下文的预期）。
    context_window: int = 1000000


@dataclass
class VisionLLMConfig:
    """视觉模型连接配置（多模态图表分析）。

    字段对齐 LLMConfig（duck-typing：可直接喂 LLMClient），但指向独立的
    视觉端点——图表看图必须引入可配置的视觉模型。
    默认 DeepSeek 视觉模型（deepseek-v4-flash-vision-exp，与文本 LLM 同一
    端点/key）；可经 PAPERFLOW_VISION_MODEL/BASE_URL 换其他 OpenAI 兼容
    端点（如智谱 GLM-4V）。api_key 留空不崩启动，由 analyze_figures 工具
    调用时降级报错。

    Attributes:
        base_url: str，视觉端点地址
        api_key: str，视觉模型密钥（留空不崩启动，调用时降级）
        model: str，视觉模型名
        max_tokens: int，单次视觉输出上限
        timeout_connect: float，连接超时（秒）
        timeout_read: float，读超时（秒）
        max_retries: int，自动重试次数
        temperature: float，采样温度
        context_window: int，上下文窗口（token）
    """

    #: 视觉端点基础地址，默认 DeepSeek（与文本 LLM 同一端点/key）
    # base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    base_url: str = "https://api.deepseek.com/v1"

    #: 视觉模型 API 密钥——**不硬编码默认值**，经 PAPERFLOW_VISION_API_KEY 提供
    api_key: str = ""

    #: 视觉模型名称（deepseek-v4-flash-vision-exp；可经 env 换回 glm-4v-flash）
    # model: str = "glm-4v-flash"
    model: str = "deepseek-v4-flash-vision-exp"

    #: 单次视觉输出上限（逐图分析，几行结构化文本，2048 足够）
    max_tokens: int = 2048

    #: LLM HTTP 连接超时（秒）——字段与 LLMConfig 对齐（LLMClient 按字段读 config）
    timeout_connect: float = 10.0

    #: LLM HTTP 读超时（秒）——逐图分析输出短，取文本 LLM 的一半
    timeout_read: float = 120.0

    #: 传输层自动重试次数
    max_retries: int = 2

    #: 采样温度，0.0 确定性输出
    temperature: float = 0.0

    #: 模型上下文窗口（token 数）
    context_window: int = 32768


# ── runtime / corpus ────────────────────────────────────────────────────────

@dataclass
class RuntimeConfig:
    """运行时基础设施：工作区、agent 插件目录、会话风险阈值。

    Attributes:
        workspace: str，运行时数据根目录（milvus/memory/audit 等）
        agents_dir: str，Agent 插件扫描目录
        max_risk: str，会话风险阈值（超过即被 PolicyEngine 拦截）
    """

    #: 运行时数据根目录，存放 milvus、memory、audit 等
    workspace: str = "data"

    #: Agent 插件扫描目录，默认扫描项目根下的 agents/
    agents_dir: str = "agents"

    #: 会话风险阈值（工具 risk_level 超过此值即被 PolicyEngine 拦截，
    #: 取值 ∈ RISK_ORDER 的键，如 "medium" / "high"）
    max_risk: str = "medium"


@dataclass
class CorpusConfig:
    """语料库与产物路径。个人绝对路径，经 config.yaml / env 提供，留空走各自回退。

    Attributes:
        note_dir: str，笔记目录（产出落点，不是索引源）
        pdf_dir: str，PDF 目录（RAG 索引源）
        research_dir: str，研究产物目录（空则回退 workspace/research）
        citations_bib_path: str，references.bib 路径（引用库真相源；空则回退默认）
    """

    #: 语料库笔记目录（笔记产物的默认落点；不进检索知识库,note/）——留空则文件类工具无可用根。
    note_dir: str = ""

    #: 语料库 PDF 目录（RAG 索引源,pdf/）。
    pdf_dir: str = ""

    #: 研究产物目录（产物区,research/）——空则由 factory 回退 workspace/research。
    research_dir: str = ""

    #: references.bib 路径（引用库真相源）。空则回退 workspace/citations/references.bib
    citations_bib_path: str = ""


# ── intent ──────────────────────────────────────────────────────────────────

@dataclass
class IntentJevConfig:
    """判定服务（经 Vercel AI Gateway 调决策模型）配置。

    网关是托管 API：账户**必须先绑定信用卡**才服务请求（连免费额度也要先绑），
    未完成验证的账户一律 403。因此启用意图层要求启动探测通过，见 `cli.py`。

    Attributes:
        base_url: str，网关地址（Decision 模态走 `/v1/evaluate`）
        api_key: str，网关 key（`vck_` 前缀）；留空则该层不可用
        model: str，模型名（`<厂商>/<模型>`）。同一请求形态下有多家可选实现，
            换实现只改这一项
        timeout: float，单次调用读超时（秒）
        max_retries: int，可恢复错误（429/5xx/超时）的重试次数
        zero_data_retention: bool，请求零数据保留。本层会把**用户对话史**发出去，
            因此默认开启；被服务端拒绝时按「该层不可用」处理，**不静默摘掉**这一项
        only_provider: str，钉住服务提供方（避免被路由到别的实现）；留空不钉
    """

    base_url: str = "https://ai-gateway.vercel.sh/v1"
    api_key: str = ""
    model: str = "typesafe-ai/jev"
    #: 单次调用读超时（秒）
    timeout: float = 5.0
    #: 可恢复错误的重试次数（次）
    max_retries: int = 1
    #: 零数据保留（默认开启：本层发送的是用户对话史）
    zero_data_retention: bool = True
    #: 钉住的服务提供方（空串 = 不钉，由网关路由）
    only_provider: str = "typesafe-ai"


@dataclass
class IntentConfig:
    """意图识别子系统配置：总开关 + 判定用的历史窗口 + 判定服务。

    Attributes:
        enabled: bool，意图识别总开关（关时整套意图层不装配：不装载知识库、
            Agent 走纯 ReAct，提示词不含意图规则）
        history_messages: int，判定时参考的最近对话条数（运行时按它截历史切片）
        jev: IntentJevConfig，判定服务配置（规则层不命中时才用得上）
    """

    #: 意图识别总开关（默认关：系统默认形态是纯 ReAct；显式开启才挂载意图层）
    enabled: bool = False

    #: 判定时参考的最近若干轮对话（只取 user/assistant 文本，不含工具结果）
    history_messages: int = 6

    jev: IntentJevConfig = field(default_factory=IntentJevConfig)


# ── rag ─────────────────────────────────────────────────────────────────────

@dataclass
class EmbeddingConfig:
    """RAG 检索栈云端嵌入配置。

    仅云端：api_key 缺失不阻塞启动，由调用方按降级语义处理（路由退稀疏、
    检索跳稠密路）。

    batch_size/timeout/max_retries 是 RagEmbedder 传输参数（改它们不改变
    向量结果，无需重建索引）。精排连接在 rag.rerank，本段只负责嵌入。

    Attributes:
        base_url: str，嵌入端点地址
        api_key: str，密钥（缺失不阻塞启动，降级处理）
        embed_model: str，嵌入模型名
        batch_size: int，单批文本条数
        timeout: float，读超时（秒）
        max_retries: int，可恢复错误重试次数
    """
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    embed_model: str = "Qwen/Qwen3-Embedding-0.6B"
    #: 单批嵌入请求的文本条数（条）
    batch_size: int = 32
    #: 嵌入 HTTP 读超时（秒）
    timeout: float = 60.0
    #: 可恢复错误（连接错误/超时/5xx）的重试次数（次）
    max_retries: int = 2


@dataclass
class RerankConfig:
    """精排模型独立连接配置——与 embedding 的传输参数解耦。

    base_url/api_key 留空 = 继承 rag.embedding 同名字段，from_env 阶段解析完毕，
    装配侧拿到的是已合并值（与 intent 的继承解析同一阶段完成）。
    timeout/max_retries 与 embedding 的同名字段解耦，改精排超时不牵动嵌入。

    Attributes:
        base_url: str，精排端点（留空继承 rag.embedding）
        api_key: str，密钥（留空继承 rag.embedding）
        model: str，精排模型名
        timeout: float，读超时（秒；与嵌入解耦）
        max_retries: int，可恢复错误重试次数
    """
    base_url: str = ""
    api_key: str = ""
    model: str = "Qwen/Qwen3-Reranker-0.6B"
    #: 精排 HTTP 读超时（秒）
    timeout: float = 60.0
    #: 可恢复错误的重试次数（次）
    max_retries: int = 2


@dataclass
class RetrieverConfig:
    """混合检索参数（改动需重评检索质量）。

    Attributes:
        top_k: int，默认返回块数
        bm25_topk: int，BM25 粗召回数
        vector_topk: int，向量粗召回数
        rerank_candidates: int，重排候选池下限（实际池 = max(top_k*2, 本值)）
        rrf_k: int，RRF 融合常数 k（越大分数差越小、融合越平滑）
    """

    #: 默认返回块数（条）。
    top_k: int = 5
    #: BM25 粗召回数（条）。
    bm25_topk: int = 30
    #: 向量粗召回数（条）。
    vector_topk: int = 30
    #: 重排候选池下限（条）；实际池大小 = max(top_k * 2, 本值)。
    rerank_candidates: int = 24
    #: RRF 融合常数 k（无量纲；越大相邻名次间的分数差越小、融合越平滑）。
    rrf_k: int = 30


@dataclass
class QueryRewriteConfig:
    """query 改写模型连接与行为参数。

    base_url/api_key 留空逐项继承 llm 同名字段；model 留空沿用主模型。

    Attributes:
        base_url: str，改写模型端点（留空继承 llm）
        api_key: str，密钥（留空继承 llm）
        model: str，模型名（留空沿用主模型）
        history_messages: int，喂给 condense 的最近对话条数
        rewrite_num: int，prompt 要求的改写变体条数
        max_queries: int，最终查询集封顶（含原 query）
        max_query_chars: int，单条改写查询字符上限（超限丢弃）
    """
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    #: 喂给改写（condense）的最近对话消息条数（条）。
    history_messages: int = 6
    #: prompt 要求的改写变体条数（条）：改它改变改写 prompt 与查询集规模，需重评。
    rewrite_num: int = 1
    #: 最终查询集封顶（条，含原 query）：生成侧最多占 max_queries-1 席，最后 1 席留给原 query。
    max_queries: int = 2
    #: 单条改写查询的字符上限（字符）：超过视为 LLM 输出异常并丢弃。
    max_query_chars: int = 200


@dataclass
class ChunkerConfig:
    """切块参数（改动改变切块结果 → 配方哈希自动触发全量重索引）。

    Attributes:
        max_tokens: int，每块最大 token 数
        overlap_tokens: int，相邻块重叠 token 数
    """

    #: 每块最大 token 数（token，按 core.tokenization 近似计数）。
    max_tokens: int = 512
    #: 相邻块重叠 token 数（token），约为块长的 1/8。
    overlap_tokens: int = 64


@dataclass
class StorageConfig:
    """Milvus 向量库连接配置。

    Attributes:
        uri: str，Milvus 连接地址（本地路径走 Lite，http 走 Standalone）
        collection: str，集合名
        batch_size: int，分页遍历每页行数
        timeout: float，读路径单次 RPC 截止时间（秒）
        write_timeout: float，写路径单次 RPC 截止时间（秒）
    """

    #: Milvus 连接地址。本地文件路径 → Milvus Lite（内嵌，单测用）；
    #: ``http://host:19530`` → Milvus Standalone（生产默认）。
    uri: str = "http://localhost:19530"

    #: Milvus 集合名（单一集合，对应迁移前的向量库 collection）
    collection: str = "paperflow"

    #: all_documents 分页遍历每页行数（行；规避单次 query 16384 行上限）。
    batch_size: int = 1000

    #: 读路径（search / query / 全量遍历 / 统计 / 集合加载）单次 RPC 的超时（秒）。
    #: 必须设：不设时 pymilvus 走默认重试策略（最多 75 次、退避到 3 秒），服务不可达时
    #: 一次检索能白等好几分钟。这个值同时管两件事——每次尝试的 gRPC 截止时间，以及整个
    #: 重试循环的时间预算（pymilvus 从同一个 timeout 参数取两者），所以设了就等于给这次
    #: 调用封了顶，失败立刻回到上层由熔断器判断。
    timeout: float = 5.0

    #: 写路径（upsert / flush / delete / 建集合）单次 RPC 的超时（秒）。
    #: 比读路径宽松：批量入库本身就要若干秒，用读路径那档会把合法写入判成超时。
    write_timeout: float = 60.0


@dataclass
class RagToolsConfig:
    """RAG 工具输出参数。

    Attributes:
        excerpt_chars: int，单条命中正文摘录上限（字符）
    """

    #: 单条命中正文摘录上限（字符）。
    excerpt_chars: int = 400


@dataclass
class RagConfig:
    """RAG 检索栈配置（按子模块分区，与 config.yaml ``rag:`` 段同构）。

    Attributes:
        embedding: EmbeddingConfig，嵌入连接
        rerank: RerankConfig，精排连接
        retriever: RetrieverConfig，混合检索参数
        query_rewrite: QueryRewriteConfig，query 改写
        chunker: ChunkerConfig，切块参数（改动触发配方哈希全量重索引）
        storage: StorageConfig，向量库连接
        tools: RagToolsConfig，检索工具输出参数
    """

    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)
    retriever: RetrieverConfig = field(default_factory=RetrieverConfig)
    query_rewrite: QueryRewriteConfig = field(default_factory=QueryRewriteConfig)
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    tools: RagToolsConfig = field(default_factory=RagToolsConfig)


# ── memory / session / agents ───────────────────────────────────────────────

@dataclass
class MemoryConfig:
    """记忆系统配置。

    Attributes:
        sleeptime_enable: bool，Sleeptime 后台整合开关
        sleeptime_agent_frequency: int，每 N 条新消息检查一次
    """

    #: Sleeptime 后台整合开关
    sleeptime_enable: bool = True

    #: Sleeptime 触发频率（每 N 条新消息检查一次）
    sleeptime_agent_frequency: int = 50


@dataclass
class SessionConfig:
    """会话恢复配置。

    Attributes:
        resume_replay: bool，--resume 时是否把历史回放进终端滚动区
        resume_replay_limit: int，回放条数上限（0 = 整窗）
    """

    #: 会话恢复时把历史对话回放进终端滚动区。--resume 恢复的是模型上下文，
    #: 屏幕上否则不留任何痕迹（用户会以为恢复失败）；见 terminal/resume.py。
    #: 置 False 则只恢复上下文、不显示历史。
    resume_replay: bool = True

    #: 回放条数上限（取窗口末尾 N 条）。0 = 回放整个 in-context 窗口。
    resume_replay_limit: int = 0


@dataclass
class AgentsConfig:
    """子 agent 配置。

    timeouts 是自由 dict（agent 类型 → 秒数），仅 YAML 可配（dict 无自然 env 形态）。
    各值按该 agent 完整任务的典型时长留余量设定：note-agent 覆盖含内审重试的纯笔记
    端到端，paper-agent 覆盖大批量新颖性检索，review-agent 覆盖全文审阅，research-agent
    覆盖完整研究链路；paper-agent 还要覆盖读整篇与图表问题，citation-agent 要覆盖
    逐篇读首页取元数据加查引/入库/渲染一整套，memory-agent 要覆盖记忆与清单的读写。
    表里没有的类型落回 SpawnSubAgentTool.timeout 的类默认（120s）——新增角色时一并补进
    本表，别让它在表外静默用类默认。某 agent 反复撞帽说明任务时长需要重新评估，
    而不是继续调大。

    Attributes:
        timeouts: dict[str, int]，agent 类型 → 超时秒数（仅 YAML 可配；按各 agent 完整任务典型时长留余量）
    """

    timeouts: dict[str, int] = field(
        default_factory=lambda: {
            "note-agent": 900, "paper-agent": 420, "review-agent": 300,
            "research-agent": 1800, "rag-agent": 900,
            "citation-agent": 300, "memory-agent": 180,
        })


_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class McpServerConfig:
    """单个 MCP server 的接入配置（config.yaml 顶层 mcp_servers 段；仅 YAML，无环境变量形态）。

    过滤顺序 allowed → disabled →
    风险分级；写类工具默认禁用，write_tools 显式开启；uvx 冷启动可能下载包，
    connect_timeout 默认高于业界 5s。

    Attributes:
        transport: str，传输方式：stdio | http
        command: str，stdio 必填：可执行文件
        args: list[str]，stdio 启动参数
        env: dict[str, str]，stdio 子进程环境变量
        url: str，http 必填
        headers: dict[str, str]，http 请求头
        enabled: bool，是否启用该 server
        agents: list[str]，可注入的 agent 类型名单
        connect_timeout: float，连接超时（秒）
        call_timeout: float，单次调用超时（秒）
        disabled_tools: list[str]，显式屏蔽的工具名
        allowed_tools: list[str] | None，白名单（None = 不限制）
        write_tools: list[str]，预批准写入的工具（免逐次确认）
    """

    transport: str = "stdio"            # "stdio" | "http"
    command: str = ""                   # stdio 必填：可执行文件（如 uvx）
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""                       # http 必填
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    agents: list[str] = field(default_factory=lambda: ["paper-agent"])
    connect_timeout: float = 30.0
    call_timeout: float = 120.0
    disabled_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] | None = None
    write_tools: list[str] = field(default_factory=list)

    def validate(self, name: str) -> None:
        """校验单个 server 配置：名字格式、transport 取值、以及各 transport 的必填项。

        Args:
            name: str，server 名（用于错误信息与格式校验）
        """
        if not _SERVER_NAME_RE.fullmatch(name):
            raise ValueError(f"MCP server 名非法: '{name}'（须匹配 [a-zA-Z0-9_-]+）")
        if self.transport not in ("stdio", "http"):
            raise ValueError(f"MCP server '{name}': 非法 transport '{self.transport}'（stdio|http）")
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"MCP server '{name}': transport=stdio 必须提供 command")
        if self.transport == "http" and not self.url.startswith(("http://", "https://")):
            raise ValueError(f"MCP server '{name}': transport=http 必须提供 http(s) url")


def parse_mcp_servers(raw: dict | None) -> dict[str, McpServerConfig]:
    """yaml 原始 dict → 校验后的 McpServerConfig 表；未知键忽略（同仓库 hasattr 守卫精神）。

    Args:
        raw: dict | None，YAML 里 mcp_servers 段的原始值

    Returns:
        server 名 → 校验通过的 McpServerConfig；raw 为 None 返回空表。
    """
    # 顶层类型守卫：用户写成 mcp_servers: [a, b]（列表）或字符串时，直接
    # .items() 会抛裸 AttributeError——改为干净的 ValueError。
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"mcp_servers: 顶层配置必须是映射（server 名 → 配置段），得到 {type(raw).__name__}")
    known = {f.name for f in fields(McpServerConfig)}
    servers: dict[str, McpServerConfig] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            raise ValueError(f"MCP server '{name}': 配置段必须是映射")
        cfg = McpServerConfig(**{k: v for k, v in item.items() if k in known})
        cfg.validate(name)
        servers[name] = cfg
    return servers


# ── 通用合并原语 ────────────────────────────────────────────────────────────

def _is_scalar(val) -> bool:
    """标量判定：str/int/float/bool/None——只有这些才允许 YAML/env 覆写。

    非 dataclass、非 dict/list 的自定义对象（如 ``compaction`` 的
    ``CompactionSettings`` 普通类实例）没有通用覆写语义：把 YAML/env 值直接
    setattr 成裸 dict/str 会静默破坏其行为，必须跳过。

    Args:
        val: 任意字段当前值

    Returns:
        True 表示是可被 YAML/env 覆写的标量（str/int/float/bool/None）。
    """
    return val is None or isinstance(val, (str, int, float, bool))


def _coerce(current, val):
    """把标量 val 转成与 current 同类的类型（env 恒字符串 / YAML 可能写错类型）。

    - bool：字符串按 "1"/"true"/"yes" 判真（"false" 必须落 False）；
    - int / float：直接转换；
    - str：原样保留；
    - 其余（dict/list 等）：原样。

    Args:
        current: 字段当前值（提供目标类型）
        val: YAML/env 来的新值（env 恒为字符串）

    Returns:
        转换到与 current 同类型的值（dict/list 等原样返回）。
    """
    if isinstance(current, bool):
        return val.lower() in ("1", "true", "yes") if isinstance(val, str) else bool(val)
    if isinstance(current, int):
        return int(val)
    if isinstance(current, float):
        return float(val)
    return val


def _merge(node, raw) -> None:
    """通用递归合并：沿 ``dataclasses.fields()`` 下行，raw 覆盖 node。

    - 字段当前值是 dataclass → 递归（要求 raw 为 dict，否则跳过）；
    - 自由 dict/list 字段 → 整体赋值；
    - 标量 → 按目标字段当前类型转换（``_coerce``）；
    - 其余自定义对象（``compaction``）→ 跳过（见 ``_is_scalar``）；
    - raw 里的未知键被忽略（沿用现有 hasattr 守卫精神，运行期不因陌生键崩）。

    Args:
        node: 任意 dataclass 实例（被就地覆写）
        raw: dict，YAML 覆盖值（非 dict 直接返回）
    """
    if not isinstance(raw, dict):
        return
    for f in fields(node):
        if f.name not in raw:
            continue
        cur = getattr(node, f.name)
        val = raw[f.name]
        if is_dataclass(cur) and not isinstance(cur, type):
            _merge(cur, val)
        elif isinstance(cur, (dict, list)):
            setattr(node, f.name, val)
        elif _is_scalar(cur):
            setattr(node, f.name, _coerce(cur, val))
        # else：非 dataclass 非集合非标量的自定义对象（compaction）跳过覆写，
        #       不得把 YAML 值 setattr 成裸 dict/str。


@dataclass
class PaperFlowConfig:
    """项目全局配置,聚合所有子系统的配置项。

    ``runtime.workspace`` 是运行时数据根目录,各子系统的数据写入统一走此路径。

    Attributes:
        llm: LLMConfig，LLM 连接
        vision: VisionLLMConfig，视觉模型连接
        runtime: RuntimeConfig，运行时基础设施
        corpus: CorpusConfig，语料库与产物路径
        intent: IntentConfig，意图识别
        rag: RagConfig，RAG 检索栈
        memory: MemoryConfig，记忆系统
        session: SessionConfig，会话恢复
        agents: AgentsConfig，子 agent 配置
        compaction: CompactionSettings，上下文压缩
        mcp_servers: dict[str, McpServerConfig]，MCP server 接入表（仅 YAML）
    """

    #: LLM 连接配置
    llm: LLMConfig = field(default_factory=LLMConfig)

    #: 视觉模型连接配置（多模态图表分析，独立于文本 LLM）
    vision: VisionLLMConfig = field(default_factory=VisionLLMConfig)

    #: 运行时基础设施（workspace / agents_dir / max_risk）
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    #: 语料库与产物路径
    corpus: CorpusConfig = field(default_factory=CorpusConfig)

    #: 意图识别子系统（总开关 + 编码器 + 路由器）
    intent: IntentConfig = field(default_factory=IntentConfig)

    #: RAG 检索栈（嵌入/检索/改写/切块/索引/存储/解析/工具）
    rag: RagConfig = field(default_factory=RagConfig)

    #: 记忆系统（sleeptime 后台整合）
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    #: 会话恢复（屏上历史回放）
    session: SessionConfig = field(default_factory=SessionConfig)

    #: 子 agent 配置（timeouts 为 YAML-only 自由 dict）
    agents: AgentsConfig = field(default_factory=AgentsConfig)

    #: 上下文压缩配置（惰性工厂见 _default_compaction——延迟导入切断循环依赖）
    compaction: "CompactionSettings" = field(default_factory=_default_compaction)

    #: MCP server 接入配置（config.yaml 顶层 mcp_servers；仅 YAML，无环境变量形态）
    mcp_servers: dict[str, "McpServerConfig"] = field(default_factory=dict)

    @classmethod
    def from_env(cls, config_path: str | None = None) -> "PaperFlowConfig":
        """工厂方法：依次加载 .env 兜底、可选 YAML、环境变量覆盖。

        返回值保证所有字段有值（至少为 dataclass 默认值）。

        Args:
            config_path: str | None，config.yaml 路径；None 用默认 "config.yaml"（不存在则跳过）

        Returns:
            加载完成的配置（.env → YAML → env 覆盖，并解析留空继承与绝对化 workspace）。
        """
        # 加载 .env 文件（不覆盖已有环境变量，即 OS 环境优先于 .env）
        load_dotenv()

        config = cls()
        config._load_yaml(config_path)  # 第一步：YAML 文件（优先级最低）
        config._load_env()               # 第二步：环境变量（覆盖 YAML 值）
        # 留空继承：rag.query_rewrite ← llm、rag.rerank ← rag.embedding。
        # 必须在 YAML/env 全部加载后做——否则 env 覆盖会被继承值抢先顶掉。
        emb = config.rag.embedding
        # rag.rerank 留空逐项继承 rag.embedding（端点/key），与上面同一阶段。
        rr = config.rag.rerank
        rr.base_url = rr.base_url or emb.base_url
        rr.api_key = rr.api_key or emb.api_key
        qr = config.rag.query_rewrite
        qr.base_url = qr.base_url or config.llm.base_url
        qr.api_key = qr.api_key or config.llm.api_key
        # workspace 绝对化:相对 workspace(默认 "data")派生的根会被工作区校验二次拼接
        # 成 data/data/... 双前缀,把正确绝对路径也误拦。绝对化后所有派生根一致绝对、
        # [目录] 提示也变绝对。只在此生产入口处理——测试直接构造的值不受影响。
        config.runtime.workspace = str(
            Path(config.runtime.workspace).expanduser().resolve())
        return config

    def _load_yaml(self, config_path: str | None) -> None:
        """从可选的 config.yaml 读取配置并覆盖默认值。

        顶层键与 dataclass 字段同名（``llm`` / ``runtime`` / ``rag`` …），
        经通用递归合并 ``_merge`` 下行，任意深度；不存在的文件静默跳过，
        未知键忽略。``mcp_servers`` 需校验+转换，单独分支。

        Args:
            config_path: str | None，config.yaml 路径；文件不存在时静默跳过
        """
        path = Path(config_path or "config.yaml")
        if not path.exists():
            return

        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return

        # MCP servers：嵌套结构需校验+转换，单独处理（不参与通用合并）
        if "mcp_servers" in data:
            self.mcp_servers = parse_mcp_servers(data["mcp_servers"])
            data = {k: v for k, v in data.items() if k != "mcp_servers"}

        _merge(self, data)

    def _load_env(self) -> None:
        """
        从环境变量读取配置并覆盖 YAML / 默认值。

        env 名 = ``PAPERFLOW_`` + 配置路径（``.`` 换 ``_``）大写，如
        ``PAPERFLOW_LLM_API_KEY`` / ``PAPERFLOW_RAG_STORAGE_URI`` /
        ``PAPERFLOW_RAG_QUERY_REWRITE_MODEL``。自由 dict/list 字段
        （``agents.timeouts``、``mcp_servers``）不派生 env。
        """
        _apply_env(self, ())


def _apply_env(node, prefix: tuple[str, ...]) -> None:
    """按路径约定递归派生 env 并覆盖：仅标量字段消费 env，dict/list 跳过。

    Args:
        node: 任意 dataclass 实例（被就地覆写）
        prefix: tuple[str, ...]，当前路径前缀（拼 PAPERFLOW_* env 名）
    """
    for f in fields(node):
        cur = getattr(node, f.name)
        path = prefix + (f.name,)
        if is_dataclass(cur) and not isinstance(cur, type):
            _apply_env(cur, path)
            continue
        if isinstance(cur, (dict, list)):
            continue  # YAML-only 自由集合
        if not _is_scalar(cur):
            continue  # 非标量自定义对象（compaction）不派生 env、不覆写
        env_name = "PAPERFLOW_" + "_".join(p.upper() for p in path)
        val = os.getenv(env_name)
        if val:
            setattr(node, f.name, _coerce(cur, val))
