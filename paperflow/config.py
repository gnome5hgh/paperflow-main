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
    api_key: str = ""

    #: 模型名称，传给 API 的 model 参数
    model: str = "deepseek-chat"

    #: 单次请求最大输出 token 数
    max_tokens: int = 4096

    #: 采样温度，0.0 表示确定性输出（适合工具调用场景）
    temperature: float = 0.0


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

        # 顶层配置字段
        for key in ("workspace", "agents_dir", "max_risk"):
            if key in data:
                setattr(self, key, data[key])

    def _load_env(self) -> None:
        """
        从环境变量读取配置并覆盖 YAML / 默认值。

        支持的环境变量::

            PAPERFLOW_API_KEY     → llm.api_key
            PAPERFLOW_BASE_URL    → llm.base_url
            PAPERFLOW_MODEL       → llm.model
            PAPERFLOW_WORKSPACE   → workspace
            PAPERFLOW_AGENTS_DIR  → agents_dir
            PAPERFLOW_MAX_RISK    → max_risk
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
        }

        for env_var, (parent, attr) in env_map.items():
            val = os.getenv(env_var)
            if val:
                if parent == "llm":
                    setattr(self.llm, attr, val)
                else:
                    setattr(self, attr, val)
