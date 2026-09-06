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
from dataclasses import dataclass, field
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

    #: Obsidian vault 笔记目录(数据源 note/)——**个人绝对路径,不硬编码默认值**,
    #: 经 .env(PAPERFLOW_VAULT_NOTE_DIR)或 config.yaml 提供;留空则文件类工具无可用根。
    vault_note_dir: str = ""

    #: Obsidian vault PDF 目录(数据源 pdf/)——同 vault_note_dir,经 .env(PAPERFLOW_VAULT_PDF_DIR)
    #: 或 config.yaml 提供。
    vault_pdf_dir: str = ""

    #: Obsidian vault 研究目录(数据源 research/)——同 vault_note_dir,经 .env
    #: (PAPERFLOW_VAULT_RESEARCH_DIR)或 config.yaml 提供;空则由 factory 回退 workspace/research。
    vault_research_dir: str = ""

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

    #: 嵌入模型（真实 bge 落地，维度从模型读取不硬编码）
    #: 实际加载路径由 resolve_model_dir 解析：`<workspace>/models/<name>/` 存在则用本地
    #:（HF 权威权重存 data/models/，gitignored），否则回退此 HF 名（首次使用自动下载）。
    embed_model: str = "BAAI/bge-small-zh-v1.5"

    #: 重排模型（Cross-encoder）
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

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

        # 嵌套处理 llm/vision 子配置：逐个字段检查，避免类型不匹配
        for sub in ("llm", "vision"):
            if sub in data:
                for key, val in data[sub].items():
                    if hasattr(getattr(self, sub), key):
                        setattr(getattr(self, sub), key, val)

        # 顶层配置字段(含 vault / RAG 键,均可通过 config.yaml 顶层覆盖默认值)
        for key in ("workspace", "agents_dir", "max_risk",
                    "vault_note_dir", "vault_pdf_dir", "vault_research_dir",
                    "citations_bib_path",
                    "grobid_endpoint", "milvus_uri", "milvus_collection",
                    "embed_model", "rerank_model",
                    "agent_timeouts", "sleeptime_enable", "sleeptime_agent_frequency"):
            if key in data:
                setattr(self, key, data[key])

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
            PAPERFLOW_VAULT_NOTE_DIR → vault_note_dir
            PAPERFLOW_VAULT_PDF_DIR  → vault_pdf_dir
            PAPERFLOW_VAULT_RESEARCH_DIR → vault_research_dir
            PAPERFLOW_CITATIONS_BIB_PATH → citations_bib_path
            PAPERFLOW_GROBID_ENDPOINT → grobid_endpoint
            PAPERFLOW_EMBED_MODEL    → embed_model
            PAPERFLOW_RERANK_MODEL   → rerank_model
            PAPERFLOW_VISION_BASE_URL → vision.base_url
            PAPERFLOW_VISION_API_KEY  → vision.api_key
            PAPERFLOW_VISION_MODEL    → vision.model
            PAPERFLOW_SLEEPTIME_ENABLE    → sleeptime_enable（"true"/"false"）
            PAPERFLOW_SLEEPTIME_FREQUENCY → sleeptime_agent_frequency
        """
        # 映射表：环境变量名 → (父对象名, 属性名)
        # parent 为 "llm"/"vision" 表示写入 self.<parent>.<attr>，None 表示写入 self.<attr>
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
            "PAPERFLOW_VAULT_NOTE_DIR": (None, "vault_note_dir"),
            "PAPERFLOW_VAULT_PDF_DIR": (None, "vault_pdf_dir"),
            "PAPERFLOW_VAULT_RESEARCH_DIR": (None, "vault_research_dir"),
            "PAPERFLOW_CITATIONS_BIB_PATH": (None, "citations_bib_path"),
            "PAPERFLOW_GROBID_ENDPOINT": (None, "grobid_endpoint"),
            "PAPERFLOW_MILVUS_URI": (None, "milvus_uri"),
            "PAPERFLOW_MILVUS_COLLECTION": (None, "milvus_collection"),
            "PAPERFLOW_EMBED_MODEL": (None, "embed_model"),
            "PAPERFLOW_RERANK_MODEL": (None, "rerank_model"),
            "PAPERFLOW_SLEEPTIME_ENABLE": (None, "sleeptime_enable"),
            "PAPERFLOW_SLEEPTIME_FREQUENCY": (None, "sleeptime_agent_frequency"),
        }

        for env_var, (parent, attr) in env_map.items():
            val = os.getenv(env_var)
            if val:
                obj = getattr(self, parent) if parent in ("llm", "vision") else self
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
