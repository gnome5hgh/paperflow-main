# paperflow/config.py
"""
全局配置模块，提供 LLM 连接参数和项目运行时配置。

配置加载优先级（从低到高）：
    1. dataclass 默认值（本文件是所有可调参数值的**唯一声明点**——改默认值只改这里）
    2. config.yaml（可选，文件不存在则跳过；纯覆盖文件，不重复声明默认值）
    3. 环境变量（最高优先级，按 ``PAPERFLOW_`` + 配置路径大写派生）

配置结构与 config.yaml 同构（spec 2026-10-05-constants-and-config-reorg §4/§6）：
``runtime`` / ``corpus`` / ``intent{encoder, router}`` / ``rag{embedding, rerank,
retriever, query_rewrite, chunker, indexer, storage, grobid, tools}`` / ``memory`` /
``session`` / ``agents{timeouts}``，外加保留的顶层 ``llm`` / ``vision`` / ``mcp_servers``。

env 名约定：字段路径以 ``_`` 连接并大写，前缀 ``PAPERFLOW_``。例如
``rag.storage.uri`` → ``PAPERFLOW_RAG_STORAGE_URI``，
``intent.router.alpha`` → ``PAPERFLOW_INTENT_ROUTER_ALPHA``。无例外表；
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


# ── constants.py 的惰性访问器 ────────────────────────────────────────────────
# 已退役（2026-10-06）：可调参数默认值曾引用各模块 constants.py，为绕开
# config→rag→config 循环导入需惰性访问器。默认值改在本文件字面量声明后，
# constants.py 只剩结构契约（正则/词表/schema/修订号），不再被本文件引用。


@dataclass
class LLMConfig:
    """
    LLM 连接配置，封装 OpenAI-compatible API 所需的所有参数。

    默认值指向 DeepSeek API（通过 OpenAI SDK 兼容层调用），
    修改 base_url 可切换到任意兼容服务（如 OpenAI、vLLM、Ollama 等）。
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
    """运行时基础设施：工作区、agent 插件目录、会话风险阈值。"""

    #: 运行时数据根目录，存放 milvus、memory、audit、templates 等
    workspace: str = "data"

    #: Agent 插件扫描目录，默认扫描项目根下的 agents/
    agents_dir: str = "agents"

    #: 会话风险阈值（工具 risk_level 超过此值即被 PolicyEngine 拦截，
    #: 取值 ∈ RISK_ORDER 的键，如 "medium" / "high"）
    max_risk: str = "medium"


@dataclass
class CorpusConfig:
    """语料库与产物路径。个人绝对路径，经 config.yaml / env 提供，留空走各自回退。"""

    #: 语料库笔记目录（RAG 索引源,note/）——留空则文件类工具无可用根。
    note_dir: str = ""

    #: 语料库 PDF 目录（RAG 索引源,pdf/）。
    pdf_dir: str = ""

    #: 研究产物目录（产物区,research/）——空则由 factory 回退 workspace/research。
    research_dir: str = ""

    #: references.bib 路径（引用库真相源）。空则回退 workspace/citations/references.bib
    citations_bib_path: str = ""


# ── intent ──────────────────────────────────────────────────────────────────

@dataclass
class IntentEncoderConfig:
    """意图路由独立稠密编码器（与 RAG 解耦，为换编码器实验留口）。

    base_url/api_key 留空 = 继承 rag.embedding 同名字段，from_env 阶段解析完毕，
    装配侧拿到的是已合并值。传输参数（batch_size/timeout/max_retries）与
    rag.embedding 同形同默认——独立实例，互不共享。
    """
    base_url: str = ""
    api_key: str = ""
    model: str = "Qwen/Qwen3-Embedding-0.6B"
    #: 单批嵌入请求的文本条数（条）
    batch_size: int = 32
    #: 嵌入 HTTP 读超时（秒）
    timeout: float = 60.0
    #: 可恢复错误（连接错误/超时/5xx）的重试次数（次）
    max_retries: int = 2


@dataclass
class RouterConfig:
    """混合路由器装配参数。alpha/top_k 是标定产物，默认值在本文件单点声明
    （标定脚本 scripts/intent/calibration/apply_calibration.py 会就地改写）。"""

    #: 稠密分支权重 alpha（稀疏路权重 1-alpha）。2026-10-05 标定值。
    alpha: float = 0.15

    #: 路由器每次查询检索的 utterances 条数（条）。2026-10-05 标定值。
    top_k: int = 3


@dataclass
class IntentConfig:
    """意图识别子系统配置：独立编码器 + 路由器。"""

    encoder: IntentEncoderConfig = field(default_factory=IntentEncoderConfig)
    router: RouterConfig = field(default_factory=RouterConfig)


# ── rag ─────────────────────────────────────────────────────────────────────

@dataclass
class EmbeddingConfig:
    """RAG 检索栈云端嵌入配置（spec 2026-10-05-embedding-cloud-startup §6）。

    云端 only——本地 sentence-transformers 已退役，api_key 缺失不阻塞启动，
    由调用方按降级语义处理（路由退稀疏、检索跳稠密路）。

    batch_size/timeout/max_retries 是 CloudEmbedder 传输参数（改它们不改变
    向量结果，无需重建索引）。精排连接已拆到 rag.rerank（spec 2026-10-05b），
    本段只负责嵌入。
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
    """精排模型独立连接配置（spec 2026-10-05b §2）——与 embedding 的传输参数解耦。

    base_url/api_key 留空 = 继承 rag.embedding 同名字段，from_env 阶段解析完毕，
    装配侧拿到的是已合并值（增量继承语义与 intent.encoder 一致）。
    timeout/max_retries 与 embedding 的同名字段解耦，改精排超时不牵动嵌入。
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
    """混合检索参数（改动需重评检索质量；标定记录见 scripts/rag/calibration/）。"""

    #: 默认返回块数（条）。
    top_k: int = 5
    #: BM25 粗召回数（条）。
    bm25_topk: int = 30
    #: 向量粗召回数（条）。
    vector_topk: int = 30
    #: 重排候选池下限（条）；实际池大小 = max(top_k * 2, 本值)，倍率是
    #: rag.constants.RERANK_CANDIDATE_MULTIPLIER（结构常量，不可配）。
    rerank_candidates: int = 24
    #: RRF 融合常数 k（无量纲；2026-10-05 标定，证据 scripts/rag/calibration/results/rrf_k_frozen.json）。
    rrf_k: int = 30


@dataclass
class QueryRewriteConfig:
    """query 改写模型完整三元组（此前只有模型名可配，端点/key 恒继承主 LLM）。

    字段留空逐项继承 llm 同名字段；model 留空 = 沿用主模型（历史行为）。
    """
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    #: 喂给改写（condense）的最近对话消息条数（条）。
    history_messages: int = 6


@dataclass
class ChunkerConfig:
    """切块参数（改动改变切块结果 → 配方哈希自动触发全量重索引）。"""

    #: 每块最大 token 数（token，按 core.tokenization 近似计数）。
    max_tokens: int = 512
    #: 相邻块重叠 token 数（token），约为块长的 1/8。
    overlap_tokens: int = 64


@dataclass
class IndexerConfig:
    """索引器参数。"""

    #: 表格块文本截断上限（字符；Milvus text 字段 65535 的防御性截断）。
    table_text_limit: int = 8000


@dataclass
class StorageConfig:
    """Milvus 向量库连接配置。"""

    #: Milvus 连接地址。本地文件路径 → Milvus Lite（内嵌，单测用）；
    #: ``http://host:19530`` → Milvus Standalone（生产默认）。
    uri: str = "http://localhost:19530"

    #: Milvus 集合名（单一集合，对应迁移前的向量库 collection）
    collection: str = "paperflow"

    #: all_documents 分页遍历每页行数（行；规避单次 query 16384 行上限）。
    batch_size: int = 1000


@dataclass
class GrobidConfig:
    """GROBID PDF 解析服务配置。"""

    #: GROBID 服务地址——RAG PDF 解析与 TitleExtractor 标题提取共用同一端点
    endpoint: str = "http://localhost:8070"

    #: 请求超时（秒），覆盖健康检查与全文解析请求。
    timeout: float = 60.0


@dataclass
class RagToolsConfig:
    """RAG 工具输出参数。"""

    #: 单条命中正文摘录上限（字符）。
    excerpt_chars: int = 400


@dataclass
class RagConfig:
    """RAG 检索栈配置（按子模块分区，与 config.yaml ``rag:`` 段同构）。"""

    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)
    retriever: RetrieverConfig = field(default_factory=RetrieverConfig)
    query_rewrite: QueryRewriteConfig = field(default_factory=QueryRewriteConfig)
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    indexer: IndexerConfig = field(default_factory=IndexerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    grobid: GrobidConfig = field(default_factory=GrobidConfig)
    tools: RagToolsConfig = field(default_factory=RagToolsConfig)


# ── memory / session / agents ───────────────────────────────────────────────

@dataclass
class MemoryConfig:
    """记忆系统配置。"""

    #: Sleeptime 后台整合开关
    sleeptime_enable: bool = True

    #: Sleeptime 触发频率（每 N 条新消息检查一次）
    sleeptime_agent_frequency: int = 50


@dataclass
class SessionConfig:
    """会话恢复配置。"""

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
    默认 120s 对完整流程太短，各值由 audit 历史数据校准（2026-09-05，45 次 spawn
    实测 + research_discovery 链路分解，见
    docs/superpowers/specs/2026-09-05-agent-timeout-recalibration-design.md）：
    - noter 900:纯笔记端到端实测稳态 610-670s(含内审重试),600 帽 4/4 任务超线;
    - searcher 420:常规检索 max 130s,但新颖性大批量检索实测 1/4 撞 300s 帽;
    - reviewer 300:全文审阅类稳态 ≈185-278s,180 帽 5/7 任务撞线;
    - researcher 1800:完整链路实测 1202s 被截断,估算 1300-1500s + 余量;
    - qa-agent 180:显式化(此前隐式落 120s 类默认),精读任务留 2 倍余量。
    撞帽复测触发点:任一 agent 再撞新帽即需重新评估该值,而非继续调大。
    """

    timeouts: dict[str, int] = field(
        default_factory=lambda: {
            "noter": 900, "searcher": 420, "reviewer": 300,
            "researcher": 1800, "qa-agent": 180,
        })


_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class McpServerConfig:
    """单个 MCP server 的接入配置（config.yaml 顶层 mcp_servers 段；仅 YAML，无环境变量形态）。

    语义见 spec 2026-10-01-mcp-client-design.md §4：过滤顺序 allowed → disabled →
    风险分级；写类工具默认禁用，write_tools 显式开启；uvx 冷启动可能下载包，
    connect_timeout 默认高于业界 5s。
    """

    transport: str = "stdio"            # "stdio" | "http"
    command: str = ""                   # stdio 必填：可执行文件（如 uvx）
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""                       # http 必填
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    agents: list[str] = field(default_factory=lambda: ["searcher"])
    connect_timeout: float = 30.0
    call_timeout: float = 120.0
    disabled_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] | None = None
    write_tools: list[str] = field(default_factory=list)

    def validate(self, name: str) -> None:
        if not _SERVER_NAME_RE.fullmatch(name):
            raise ValueError(f"MCP server 名非法: '{name}'（须匹配 [a-zA-Z0-9_-]+）")
        if self.transport not in ("stdio", "http"):
            raise ValueError(f"MCP server '{name}': 非法 transport '{self.transport}'（stdio|http）")
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"MCP server '{name}': transport=stdio 必须提供 command")
        if self.transport == "http" and not self.url.startswith(("http://", "https://")):
            raise ValueError(f"MCP server '{name}': transport=http 必须提供 http(s) url")


def parse_mcp_servers(raw: dict | None) -> dict[str, McpServerConfig]:
    """yaml 原始 dict → 校验后的 McpServerConfig 表；未知键忽略（同仓库 hasattr 守卫精神）。"""
    # 顶层类型守卫：用户写成 mcp_servers: [a, b]（列表）或字符串时，直接
    # .items() 会抛裸 AttributeError——改为干净的 ValueError（finding 4）。
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
    setattr 成裸 dict/str 会静默破坏其行为，必须跳过（Task 4 评审 carry-over）。
    """
    return val is None or isinstance(val, (str, int, float, bool))


def _coerce(current, val):
    """把标量 val 转成与 current 同类的类型（env 恒字符串 / YAML 可能写错类型）。

    - bool：字符串按 "1"/"true"/"yes" 判真（"false" 必须落 False）；
    - int / float：直接转换；
    - str：原样保留；
    - 其余（dict/list 等）：原样。
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
    """
    项目全局配置,聚合所有子系统的配置项。

    ``runtime.workspace`` 是运行时数据根目录,各子系统的数据写入统一走此路径。
    """

    #: LLM 连接配置
    llm: LLMConfig = field(default_factory=LLMConfig)

    #: 视觉模型连接配置（多模态图表分析，独立于文本 LLM）
    vision: VisionLLMConfig = field(default_factory=VisionLLMConfig)

    #: 运行时基础设施（workspace / agents_dir / max_risk）
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    #: 语料库与产物路径
    corpus: CorpusConfig = field(default_factory=CorpusConfig)

    #: 意图识别子系统（编码器 + 路由器）
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
        """
        工厂方法：依次加载 .env 兜底、可选 YAML、环境变量覆盖。

        返回值保证所有字段有值（至少为 dataclass 默认值）。
        """
        # 加载 .env 文件（不覆盖已有环境变量，即 OS 环境优先于 .env）
        load_dotenv()

        config = cls()
        config._load_yaml(config_path)  # 第一步：YAML 文件（优先级最低）
        config._load_env()               # 第二步：环境变量（覆盖 YAML 值）
        # 留空继承：intent.encoder ← rag.embedding、rag.query_rewrite ← llm。
        # 必须在 YAML/env 全部加载后做——否则 env 覆盖会被继承值抢先顶掉。
        enc = config.intent.encoder
        emb = config.rag.embedding
        enc.base_url = enc.base_url or emb.base_url
        enc.api_key = enc.api_key or emb.api_key
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
        """
        从可选的 config.yaml 读取配置并覆盖默认值。

        顶层键与 dataclass 字段同名（``llm`` / ``runtime`` / ``rag`` …），
        经通用递归合并 ``_merge`` 下行，任意深度；不存在的文件静默跳过，
        未知键忽略。``mcp_servers`` 需校验+转换，单独分支。
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
        ``PAPERFLOW_INTENT_ROUTER_ALPHA``。自由 dict/list 字段
        （``agents.timeouts``、``mcp_servers``）不派生 env。
        """
        _apply_env(self, ())


def _apply_env(node, prefix: tuple[str, ...]) -> None:
    """按路径约定递归派生 env 并覆盖：仅标量字段消费 env，dict/list 跳过。"""
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
