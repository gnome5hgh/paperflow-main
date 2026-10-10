"""Tool ABC：工具接口契约与安全元数据。

``parameters`` 使用 JSON Schema 格式直接喂给 LLM 的 function calling 机制，不做中间
抽象层，保证与 OpenAI API 兼容；安全元数据的合法取值集合声明在 ``core.constants``。
"""
from abc import ABC, abstractmethod

from paperflow.core.tool.result import ToolResult


class Tool(ABC):
    """工具抽象基类，定义所有 Agent 可调用工具的接口契约。

    子类必须定义类级别的 ``name``、``description``、``parameters`` 属性，
    并实现 ``execute(**kwargs)`` 方法。

    ``parameters`` 使用 JSON Schema 格式描述参数，
    直接喂给 LLM 的 function calling / tool use 机制，
    不做中间抽象层，保证与 OpenAI API 的兼容性。

    ``risk_level`` 分级(当前仅声明,由策略引擎执行):

    - ``"low"``     只读操作,无副作用(搜索、读取)
    - ``"medium"``  写操作,影响本地文件(写入笔记、下载 PDF)
    - ``"high"``    修改/删除操作(覆盖文件、重命名)
    - ``"critical"`` 不可逆操作(删除文件)

    Attributes:
        name: str，工具名（function calling 的 tool_choice 用）
        description: str，工具描述（告诉 LLM 何时/如何用）
        parameters: dict，JSON Schema 参数定义（直接喂给 function calling）
        risk_level: str，风险等级 low/medium/high/critical
        side_effects: list[str]，副作用声明（策略引擎据此聚合风险）
        requires_confirm: bool，需用户确认（高风险操作）
        blocked_by_default: bool，默认拦截（fail-safe）
        root_hints: list[str]，语义根名提示（仅生成 [目录] 提示，不参与强制）
        output_scan: str | None，输出扫描模式（"mark" | None）
        needs_parent: bool，需父 Agent 引用（嵌套子 agent 的工具）
        wants_run_state: bool，需注入 per-run 搜索状态
        terminal: bool，终止型工具（成功执行即终结本 Agent 任务）
        _parent: Agent | None，注入的父 Agent 引用（仅 needs_parent 的工具持有）
    """

    #: 工具名称,供 LLM function calling 的 tool_choice 使用
    name: str

    #: 工具描述,告诉 LLM 何时以及如何使用此工具
    description: str

    #: JSON Schema 格式的参数定义,直接传递给 OpenAI function calling API
    parameters: dict

    #: 风险等级,策略引擎据此决定放行/拒绝/要求确认
    risk_level: str = "low"                  # ∈ RISK_LEVELS

    #: 副作用声明,值 ∈ SIDE_EFFECTS;策略引擎据此聚合风险
    side_effects: list[str] = []

    #: 需要用户确认(高风险操作),策略引擎据此进入确认分支
    requires_confirm: bool = False

    #: 默认拦截(如危险工具未配置时 fail-safe),策略引擎据此拒绝
    blocked_by_default: bool = False

    #: 语义根名提示（如 ["note", "memory"]）——仅用于 make_tools 生成 [目录] 提示，
    #: 不参与任何强制。强制边界 = 绝对路径 + 敏感路径黑名单（WorkspacePolicyMiddleware）。
    root_hints: list[str] = []

    #: 输出扫描模式,安全扫描中间件据此决定扫描方式;"mark" | None
    output_scan: str | None = None

    #: 需要父 Agent 引用（如嵌套子 agent 的工具）。默认 False——原子工具不声明。
    needs_parent: bool = False

    #: 需要 _exec_tool 注入 per-run 搜索状态（搜索类工具 opt-in；默认 False）
    wants_run_state: bool = False

    #: 终止型工具：成功执行即代表该 Agent 本轮任务终结——Agent.run
    #: 检测到 summary["terminal"] 的结果后直接结束 ReAct 循环，不再进下一轮 LLM
    #: 调用（review-agent 的 submit 类工具；重复提交是成本事故）。校验失败的结果由
    #: 工具侧不置位 terminal 标记，模型仍可修正后重试。
    terminal: bool = False

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """
        执行工具逻辑，返回结果文本和可选的结构化摘要。

        Args:
            kwargs: LLM 生成的参数，键名与 ``parameters`` schema 中的属性名一致

        Returns:
            ToolResult，包含给 LLM 的文本和给记忆系统的摘要

        Raises:
            Exception: 执行失败时由 Agent._exec_tool 捕获并转为错误 ToolResult

        """
        ...

    def effective_target_path(self, args: dict) -> str | None:
        """导出本次调用的有效目标路径（无法解析时 None）。

        会话确认键（PolicyEngineMiddleware）与同路径写锁（runtime._path_lock）
        统一经此取目标，而不直接读 ``args["path"]``——工具若有 pathless 便捷
        入口（如 write_file 的 filename+dir 模式），子类覆写为组合后的落盘
        路径，保证：同一文件无论从哪个入口写，键一致（同锁同确认）；不同
        文件键不同（各自确认、互不串锁）。返回 None（目标不存在/调用必报错
        不落盘）时确认键退化为 (工具名, None)、写锁不加持——与无 path 参数
        的确认类工具的既有防御行为一致。

        默认实现：返回 ``args["path"]``（绝大多数工具的单一入口）。

        Args:
            args: dict，已解析的工具调用参数

        Returns:
            本次调用的有效目标路径；无法解析时 None（确认键退化为 (工具名, None)、写锁不加持）。
        """
        return args.get("path")

    def attach_agent(self, agent) -> None:
        """注入父 Agent 引用（opt-in，权限最小化）。

        只有声明 ``needs_parent`` 的工具（如嵌套子 agent 的 spawn 工具）才会被
        ``Agent.__init__`` 调用；原子工具不声明、不持有父引用。默认实现只存
        ``self._parent``，子类可覆写为访问器（如读父 agent 的 session_id）。

        Args:
            agent: Agent，父 Agent 实例
        """
        self._parent = agent
