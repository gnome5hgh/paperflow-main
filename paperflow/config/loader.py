"""配置加载与派生：三级优先级 + 递归合并 + env 按路径派生。

优先级从低到高：dataclass 默认值（``sections.py``）→ ``config.yaml``（可选、纯覆盖）
→ 环境变量（``PAPERFLOW_`` + 配置路径大写，``_`` 连接）。只认标量覆写：dict/list
（``agents.timeouts`` / ``mcp_servers``）仅 YAML 可配，非标量自定义对象（``compaction``）
既不吃 env 也不吃 YAML。
"""
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

from paperflow.config.sections import (
    AgentsConfig,
    CorpusConfig,
    IntentConfig,
    LLMConfig,
    McpServerConfig,
    MemoryConfig,
    RagConfig,
    RuntimeConfig,
    SessionConfig,
    VisionLLMConfig,
    parse_mcp_servers,
)


def _default_compaction():
    """CompactionSettings 惰性导入——compaction.py 依赖 llm.py、llm.py 依赖本模块，
    顶层 import 会构成 config→compaction→llm→config 循环（部分初始化导入失败）。
    字段默认值走此工厂，把导入推迟到首次构造时，此刻 config 已完整加载。"""
    from paperflow.core.memory.services.compaction import CompactionSettings
    return CompactionSettings()


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

    #: 记忆系统（consolidation 后台整合）
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
        # workspace 绝对化:相对 workspace(默认 ".paperflow")派生的根会被工作区校验二次拼接
        # 成 .paperflow/.paperflow/... 双前缀,把正确绝对路径也误拦。绝对化后所有派生根一致绝对、
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
