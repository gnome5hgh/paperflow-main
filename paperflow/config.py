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

from paperflow.core.memory.context_config import ContextConfig


@dataclass
class LLMConfig:
    """
    LLM 连接配置，封装 OpenAI-compatible API 所需的所有参数。

    默认值指向 DeepSeek API（通过 OpenAI SDK 兼容层调用），
    修改 base_url 可切换到任意兼容服务（如 OpenAI、vLLM、Ollama 等）。
    """

    #: API 基础地址，默认为 DeepSeek 兼容端点
    base_url: str = "https://api.deepseek.com/v1"

    #: API 密钥，通过环境变量 PAPERFLOW_API_KEY 设置
    api_key: str = "sk-78758cabb688452b8230b322f15ae862"

    #: 模型名称，传给 API 的 model 参数
    model: str = "deepseek-v4-flash"

    #: 单次响应输出上限——deepseek-v4-flash 官方最大输出 384K（max_tokens 合法范围 1-393216）。
    #: 2026-08-06 修复：4096 → 393216。长笔记草稿/大参数 write_file 不再被静默截断
    #:（generate-note 流程失败的 P8 根因之一）。
    max_tokens: int = 393216

    #: 采样温度，0.0 表示确定性输出（适合工具调用场景）
    temperature: float = 0.0

    #: 模型上下文窗口——deepseek-v4-flash 官方 1M。ContextCompressor.resolve_context_size
    #: 取半窗口 = 500K → 压缩阈值 400K、reserve 50K，正常对话永不压缩（1M 上下文的预期）。
    context_window: int = 1000000


@dataclass
class PaperFlowConfig:
    """
    项目全局配置，聚合所有子系统的配置项。

    ``workspace`` 在 Layer 0 仅作占位，后续 Layer 的数据写入统一走此路径。
    """

    #: LLM 连接配置
    llm: LLMConfig = field(default_factory=LLMConfig)

    #: 运行时数据根目录，存放 chromadb、memory、audit、templates 等
    workspace: str = "data"

    #: Agent 插件扫描目录，默认扫描项目根下的 agents/
    agents_dir: str = "agents"

    #: 会话风险阈值（工具 risk_level 超过此值即被 PolicyEngine 拦截，
    #: 取值 ∈ RISK_ORDER 的键，如 "medium" / "high"）
    max_risk: str = "medium"

    #: 上下文压缩配置（触发比例、保留比例、压缩提示词等）
    context: ContextConfig = field(default_factory=ContextConfig)

    #: Obsidian vault 笔记目录（数据源 note/），绝对路径
    vault_note_dir: str = "/Users/gnomeshgh/Documents/Obsidian Vault/paper/note"

    #: Obsidian vault PDF 目录（数据源 pdf/），绝对路径
    vault_pdf_dir: str = "/Users/gnomeshgh/Documents/Obsidian Vault/paper/pdf"

    #: GROBID Docker 服务地址（PDF 结构解析）
    grobid_url: str = "http://127.0.0.1:8070"

    #: ChromaDB 持久化路径；空 = 从 workspace 推导 <workspace>/chromadb/
    chroma_path: str = ""

    #: 嵌入模型（真实 bge 落地，维度从模型读取不硬编码）
    #: 实际加载路径由 resolve_model_dir 解析：`<workspace>/models/<name>/` 存在则用本地
    #:（HF 权威权重存 data/models/，gitignored），否则回退此 HF 名（首次使用自动下载）。
    embed_model: str = "BAAI/bge-small-zh-v1.5"

    #: 重排模型（Cross-encoder）
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    #: 子 agent 超时覆盖表（D2）：generate-note 默认 600s——端到端（读+起草+写盘+
    #: ≤2 轮审稿每轮 ≤120s）远超默认 120s，旧值下必然超时→supervisor 反复重试
    #:（2026-08-06 实测）。YAML 顶层 agent_timeouts 可覆盖；dict 无 env 形态。
    agent_timeouts: dict[str, int] = field(default_factory=lambda: {"generate-note": 600})

    @property
    def chroma_dir(self) -> str:
        """ChromaDB 目录：显式配置优先，否则从 workspace 推导。"""
        return self.chroma_path or str(Path(self.workspace) / "chromadb")

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
        # workspace 绝对化：RC1 根因——相对 workspace（默认 "data"）派生相对根
        #（_root_map 的 Path(workspace)/"templates"），被 WorkspacePolicy.before 的
        # resolve_path(root, workspace) 二次拼接成 data/data/templates → 正确绝对路径
        # 也被 security_blocked。绝对化后所有派生根一致绝对、[目录] 提示变绝对。
        # 只在此生产入口处理：直接构造 PaperFlowConfig(...) 的测试值不受影响。
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

        # 嵌套处理 llm 子配置：逐个字段检查，避免类型不匹配
        if "llm" in data:
            for key, val in data["llm"].items():
                if hasattr(self.llm, key):
                    setattr(self.llm, key, val)

        # 顶层配置字段（含 Layer 2 新增的 vault / RAG 键，
        # 均可通过 config.yaml 顶层覆盖默认值）
        for key in ("workspace", "agents_dir", "max_risk",
                    "vault_note_dir", "vault_pdf_dir", "grobid_url",
                    "chroma_path", "embed_model", "rerank_model",
                    "agent_timeouts"):
            if key in data:
                setattr(self, key, data[key])

    def _load_env(self) -> None:
        """
        从环境变量读取配置并覆盖 YAML / 默认值。

        支持的环境变量::

            PAPERFLOW_API_KEY       → llm.api_key
            PAPERFLOW_BASE_URL      → llm.base_url
            PAPERFLOW_MODEL         → llm.model
            PAPERFLOW_WORKSPACE     → workspace
            PAPERFLOW_AGENTS_DIR    → agents_dir
            PAPERFLOW_MAX_RISK      → max_risk
            PAPERFLOW_VAULT_NOTE_DIR → vault_note_dir
            PAPERFLOW_VAULT_PDF_DIR  → vault_pdf_dir
            PAPERFLOW_GROBID_URL     → grobid_url
            PAPERFLOW_CHROMA_PATH    → chroma_path
            PAPERFLOW_EMBED_MODEL    → embed_model
            PAPERFLOW_RERANK_MODEL   → rerank_model
        """
        # 映射表：环境变量名 → (父对象名, 属性名)
        # parent 为 "llm" 表示写入 self.llm.<attr>，None 表示写入 self.<attr>
        env_map = {
            "PAPERFLOW_API_KEY": ("llm", "api_key"),
            "PAPERFLOW_BASE_URL": ("llm", "base_url"),
            "PAPERFLOW_MODEL": ("llm", "model"),
            "PAPERFLOW_WORKSPACE": (None, "workspace"),
            "PAPERFLOW_AGENTS_DIR": (None, "agents_dir"),
            "PAPERFLOW_MAX_RISK": (None, "max_risk"),
            "PAPERFLOW_VAULT_NOTE_DIR": (None, "vault_note_dir"),
            "PAPERFLOW_VAULT_PDF_DIR": (None, "vault_pdf_dir"),
            "PAPERFLOW_GROBID_URL": (None, "grobid_url"),
            "PAPERFLOW_CHROMA_PATH": (None, "chroma_path"),
            "PAPERFLOW_EMBED_MODEL": (None, "embed_model"),
            "PAPERFLOW_RERANK_MODEL": (None, "rerank_model"),
        }

        for env_var, (parent, attr) in env_map.items():
            val = os.getenv(env_var)
            if val:
                if parent == "llm":
                    setattr(self.llm, attr, val)
                else:
                    setattr(self, attr, val)
