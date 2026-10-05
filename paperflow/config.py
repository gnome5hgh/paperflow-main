# paperflow/config.py
"""
全局配置模块，提供 LLM 连接参数和项目运行时配置。

配置加载优先级（从低到高）：
    1. dataclass 默认值（代码中硬编码）
    2. config.yaml（可选，文件不存在则跳过）
    3. 环境变量 PAPERFLOW_*（最高优先级，覆盖前两者）

使用方式::

    config = PaperFlowConfig.from_env()          # 自动加载
    config = PaperFlowConfig.from_env("my.yaml") # 指定 YAML 路径
"""

import os
import re
from dataclasses import dataclass, field, fields
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
    """
    LLM 连接配置，封装 OpenAI-compatible API 所需的所有参数。

    默认值指向 DeepSeek API（通过 OpenAI SDK 兼容层调用），
    修改 base_url 可切换到任意兼容服务（如 OpenAI、vLLM、Ollama 等）。
    """

    #: API 基础地址，默认为 DeepSeek 兼容端点
    base_url: str = "https://api.deepseek.com/v1"

    #: API 密钥——**不硬编码默认值**。现必须经 PAPERFLOW_API_KEY env / .env /
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


@dataclass
class EmbeddingConfig:
    """RAG 检索栈云端嵌入 + 精排配置（spec 2026-10-05-embedding-cloud-startup §6）。

    云端 only——本地 sentence-transformers 已退役，api_key 缺失不阻塞启动，
    由调用方按降级语义处理（路由退稀疏、检索跳稠密路）。
    """
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    embed_model: str = "Qwen/Qwen3-Embedding-0.6B"
    rerank_model: str = "Qwen/Qwen3-Reranker-0.6B"


@dataclass
class IntentEncoderConfig:
    """意图路由独立稠密编码器（与 RAG 解耦，为换编码器实验留口）。

    base_url/api_key 留空 = 继承 embedding 同名字段，from_env 阶段解析完毕，
    装配侧拿到的是已合并值。
    """
    base_url: str = ""
    api_key: str = ""
    model: str = "Qwen/Qwen3-Embedding-0.6B"


@dataclass
class QueryRewriteConfig:
    """query 改写模型完整三元组（此前只有模型名可配，端点/key 恒继承主 LLM）。

    字段留空逐项继承 llm 同名字段；model 留空 = 沿用主模型（历史行为）。
    """
    base_url: str = ""
    api_key: str = ""
    model: str = ""


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


@dataclass
class PaperFlowConfig:
    """
    项目全局配置,聚合所有子系统的配置项。

    ``workspace`` 是运行时数据根目录,各子系统的数据写入统一走此路径。
    """

    #: LLM 连接配置
    llm: LLMConfig = field(default_factory=LLMConfig)

    #: 视觉模型连接配置（多模态图表分析，独立于文本 LLM）
    vision: VisionLLMConfig = field(default_factory=VisionLLMConfig)

    #: 运行时数据根目录，存放 milvus、memory、audit、templates 等
    workspace: str = "data"

    #: Agent 插件扫描目录，默认扫描项目根下的 agents/
    agents_dir: str = "agents"

    #: 会话风险阈值（工具 risk_level 超过此值即被 PolicyEngine 拦截，
    #: 取值 ∈ RISK_ORDER 的键，如 "medium" / "high"）
    max_risk: str = "medium"

    #: 上下文压缩配置（惰性工厂见 _default_compaction——延迟导入切断循环依赖）
    compaction: "CompactionSettings" = field(default_factory=_default_compaction)

    #: Sleeptime 后台整合开关
    sleeptime_enable: bool = True

    #: Sleeptime 触发频率（每 N 条新消息检查一次）
    sleeptime_agent_frequency: int = 50

    #: 会话恢复时把历史对话回放进终端滚动区。--resume 恢复的是模型上下文，
    #: 屏幕上否则不留任何痕迹（用户会以为恢复失败）；见 terminal/resume.py。
    #: 置 False 则只恢复上下文、不显示历史。
    resume_replay: bool = True

    #: 回放条数上限（取窗口末尾 N 条）。0 = 回放整个 in-context 窗口。
    resume_replay_limit: int = 0

    #: 语料库笔记目录（RAG 索引源,note/）——**个人绝对路径,不硬编码默认值**,
    #: 经 .env(PAPERFLOW_NOTE_DIR)或 config.yaml 提供;留空则文件类工具无可用根。
    note_dir: str = ""

    #: 语料库 PDF 目录（RAG 索引源,pdf/）——同 note_dir,经 .env(PAPERFLOW_PDF_DIR)
    #: 或 config.yaml 提供。
    pdf_dir: str = ""

    #: 研究产物目录（产物区,research/）——同 note_dir,经 .env
    #: (PAPERFLOW_RESEARCH_DIR)或 config.yaml 提供;空则由 factory 回退 workspace/research。
    research_dir: str = ""

    #: references.bib 路径（引用库真相源）。空则回退 workspace/citations/references.bib
    citations_bib_path: str = ""

    #: GROBID 服务地址——RAG PDF 解析与 TitleExtractor 标题提取共用同一端点
    #: （env PAPERFLOW_GROBID_ENDPOINT 覆盖）
    grobid_endpoint: str = "http://localhost:8070"

    #: Milvus 连接地址。本地文件路径 → Milvus Lite（内嵌，单测用）；
    #: ``http://host:19530`` → Milvus Standalone（生产默认）。
    milvus_uri: str = "http://localhost:19530"

    #: Milvus 集合名（单一集合，对应迁移前的向量库 collection）
    milvus_collection: str = "paperflow"

    #: RAG 云端嵌入 + 精排（spec 2026-10-05）
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    #: 意图路由独立编码器（留空项在 from_env 尾部回填 embedding 值）
    intent_encoder: IntentEncoderConfig = field(default_factory=IntentEncoderConfig)
    #: query 改写模型（留空项在 from_env 尾部回填 llm 值）
    query_rewrite: QueryRewriteConfig = field(default_factory=QueryRewriteConfig)

    #: 子 agent 超时覆盖表(按 agent 类型→秒数)。默认 120s 对完整流程太短,各值由
    #: audit 历史数据校准(2026-09-05,45 次 spawn 实测 + research_discovery 链路分解,
    #: 见 docs/superpowers/specs/2026-09-05-agent-timeout-recalibration-design.md):
    #: - noter 900:纯笔记端到端实测稳态 610-670s(含内审重试),600 帽 4/4 任务超线;
    #: - searcher 420:常规检索 max 130s,但新颖性大批量检索实测 1/4 撞 300s 帽;
    #: - reviewer 300:全文审阅类稳态 ≈185-278s,180 帽 5/7 任务撞线;
    #: - researcher 1800:完整链路实测 1202s 被截断,估算 1300-1500s + 余量;
    #: - qa-agent 180:显式化(此前隐式落 120s 类默认),精读任务留 2 倍余量。
    #: 撞帽复测触发点:任一 agent 再撞新帽即需重新评估该值,而非继续调大。
    #: YAML 顶层 agent_timeouts 可覆盖;dict 无环境变量形态。
    agent_timeouts: dict[str, int] = field(default_factory=lambda: {"noter": 900, "searcher": 420, "reviewer": 300, "researcher": 1800, "qa-agent": 180})

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
        # 留空继承：intent_encoder ← embedding、query_rewrite ← llm。
        # 必须在 YAML/env 全部加载后做——否则 env 覆盖会被继承值抢先顶掉。
        config.intent_encoder.base_url = config.intent_encoder.base_url or config.embedding.base_url
        config.intent_encoder.api_key = config.intent_encoder.api_key or config.embedding.api_key
        config.query_rewrite.base_url = config.query_rewrite.base_url or config.llm.base_url
        config.query_rewrite.api_key = config.query_rewrite.api_key or config.llm.api_key
        # workspace 绝对化:相对 workspace(默认 "data")派生的根会被工作区校验二次拼接
        # 成 data/data/... 双前缀,把正确绝对路径也误拦。绝对化后所有派生根一致绝对、
        # [目录] 提示也变绝对。只在此生产入口处理——测试直接构造的值不受影响。
        config.workspace = str(Path(config.workspace).expanduser().resolve())
        return config

    def _load_yaml(self, config_path: str | None) -> None:
        """
        从可选的 config.yaml 读取配置并覆盖默认值。

        YAML 顶层键 ``llm`` 映射到 ``LLMConfig`` 字段，
        其余键（如 ``workspace``）映射到 ``PaperFlowConfig`` 自身字段。
        不存在的文件静默跳过；未知键通过 ``hasattr`` 守卫忽略。
        """
        path = Path(config_path or "config.yaml")
        if not path.exists():
            return

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        # 嵌套处理 llm/vision/embedding/intent_encoder/query_rewrite 子配置：逐个字段检查，避免类型不匹配
        for sub in ("llm", "vision", "embedding", "intent_encoder", "query_rewrite"):
            if sub in data:
                for key, val in data[sub].items():
                    if hasattr(getattr(self, sub), key):
                        setattr(getattr(self, sub), key, val)

        # 顶层配置字段(含语料库 / RAG 键,均可通过 config.yaml 顶层覆盖默认值)
        for key in ("workspace", "agents_dir", "max_risk",
                    "note_dir", "pdf_dir", "research_dir",
                    "citations_bib_path",
                    "grobid_endpoint", "milvus_uri", "milvus_collection",
                    "agent_timeouts", "sleeptime_enable", "sleeptime_agent_frequency",
                    "resume_replay", "resume_replay_limit"):
            if key in data:
                setattr(self, key, data[key])

        # 兼容旧配置：`rag_query_rewrite_model` 是 query 改写只有模型名可配时代的
        # 顶层平铺键，现已收进 query_rewrite.model 三元组（spec §6）。保留此映射
        # 是为了不破坏既有 config.yaml——旧写法仍按原语义生效，无需用户改配置。
        # 显式 query_rewrite.model 优先（上面嵌套循环已写入），env 覆盖仍在其后。
        if "rag_query_rewrite_model" in data and not self.query_rewrite.model:
            self.query_rewrite.model = data["rag_query_rewrite_model"]

        # MCP servers：嵌套结构需校验+转换，单独分支（不在上方白名单循环里）
        if "mcp_servers" in data:
            self.mcp_servers = parse_mcp_servers(data["mcp_servers"])

    def _load_env(self) -> None:
        """
        从环境变量读取配置并覆盖 YAML / 默认值。

        支持的环境变量::

            PAPERFLOW_API_KEY       → llm.api_key
            PAPERFLOW_BASE_URL      → llm.base_url
            PAPERFLOW_MODEL         → llm.model
            PAPERFLOW_LLM_TIMEOUT_CONNECT → llm.timeout_connect
            PAPERFLOW_LLM_TIMEOUT_READ    → llm.timeout_read
            PAPERFLOW_LLM_MAX_RETRIES     → llm.max_retries
            PAPERFLOW_WORKSPACE     → workspace
            PAPERFLOW_AGENTS_DIR    → agents_dir
            PAPERFLOW_MAX_RISK      → max_risk
            PAPERFLOW_NOTE_DIR → note_dir
            PAPERFLOW_PDF_DIR  → pdf_dir
            PAPERFLOW_RESEARCH_DIR → research_dir
            PAPERFLOW_CITATIONS_BIB_PATH → citations_bib_path
            PAPERFLOW_GROBID_ENDPOINT → grobid_endpoint
            PAPERFLOW_RAG_QUERY_REWRITE_MODEL → query_rewrite.model
            PAPERFLOW_VISION_BASE_URL → vision.base_url
            PAPERFLOW_VISION_API_KEY  → vision.api_key
            PAPERFLOW_VISION_MODEL    → vision.model
            PAPERFLOW_SLEEPTIME_ENABLE    → sleeptime_enable（"true"/"false"）
            PAPERFLOW_SLEEPTIME_FREQUENCY → sleeptime_agent_frequency
            PAPERFLOW_RESUME_REPLAY       → resume_replay（"true"/"false"）
            PAPERFLOW_RESUME_REPLAY_LIMIT → resume_replay_limit（0 = 整窗）
        """
        # 映射表：环境变量名 → (父对象名, 属性名)
        # parent 为 "llm"/"vision"/"query_rewrite" 表示写入 self.<parent>.<attr>，
        # None 表示写入 self.<attr>
        env_map = {
            "PAPERFLOW_API_KEY": ("llm", "api_key"),
            "PAPERFLOW_BASE_URL": ("llm", "base_url"),
            "PAPERFLOW_MODEL": ("llm", "model"),
            "PAPERFLOW_LLM_TIMEOUT_CONNECT": ("llm", "timeout_connect"),
            "PAPERFLOW_LLM_TIMEOUT_READ": ("llm", "timeout_read"),
            "PAPERFLOW_LLM_MAX_RETRIES": ("llm", "max_retries"),
            "PAPERFLOW_VISION_BASE_URL": ("vision", "base_url"),
            "PAPERFLOW_VISION_API_KEY": ("vision", "api_key"),
            "PAPERFLOW_VISION_MODEL": ("vision", "model"),
            "PAPERFLOW_WORKSPACE": (None, "workspace"),
            "PAPERFLOW_AGENTS_DIR": (None, "agents_dir"),
            "PAPERFLOW_MAX_RISK": (None, "max_risk"),
            "PAPERFLOW_NOTE_DIR": (None, "note_dir"),
            "PAPERFLOW_PDF_DIR": (None, "pdf_dir"),
            "PAPERFLOW_RESEARCH_DIR": (None, "research_dir"),
            "PAPERFLOW_CITATIONS_BIB_PATH": (None, "citations_bib_path"),
            "PAPERFLOW_GROBID_ENDPOINT": (None, "grobid_endpoint"),
            "PAPERFLOW_MILVUS_URI": (None, "milvus_uri"),
            "PAPERFLOW_MILVUS_COLLECTION": (None, "milvus_collection"),
            "PAPERFLOW_RAG_QUERY_REWRITE_MODEL": ("query_rewrite", "model"),
            "PAPERFLOW_SLEEPTIME_ENABLE": (None, "sleeptime_enable"),
            "PAPERFLOW_SLEEPTIME_FREQUENCY": (None, "sleeptime_agent_frequency"),
            "PAPERFLOW_RESUME_REPLAY": (None, "resume_replay"),
            "PAPERFLOW_RESUME_REPLAY_LIMIT": (None, "resume_replay_limit"),
        }

        for env_var, (parent, attr) in env_map.items():
            val = os.getenv(env_var)
            if val:
                obj = getattr(self, parent) if parent in ("llm", "vision", "query_rewrite") else self
                # 环境变量恒为字符串：按目标字段当前类型做布尔/整数转换，
                # 否则 bool 字段收到 "false" 会被当真值、int 字段收到 "10" 仍是字符串
                current = getattr(obj, attr)
                if isinstance(current, bool):
                    val = val.lower() in ("1", "true", "yes")
                elif isinstance(current, int):
                    val = int(val)
                elif isinstance(current, float):
                    val = float(val)
                setattr(obj, attr, val)
