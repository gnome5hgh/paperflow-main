# agents/generate-note/tools.py（8 工具完整装配：5 原子工具 + ReviewDraftTool + glob/grep）
"""generate-note 的工具装配。

5 个原子工具（read_file/read_pdf/write_file/edit_file/mark_read）复用
paperflow/tools/ 的集中式安全边界（WorkspacePolicy 白名单、风险语义）；
ReviewDraftTool 是"集中式原子工具"约定的刻意例外——定义在 agent 目录而非
paperflow/tools/：单消费者（仅 generate-note）、需 parent 注入（needs_parent）。
它还是 Layer 4 spawn 的种子：届时 SpawnSubAgentTool 同样落 paperflow/tools/spawn.py，
内部"实例化子 agent → run"原样保留，只换 wrapper（SubAgentResult + 参数化 agent 名 + allowed_spawns）。
"""
import asyncio
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.factory import make_tools
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, WriteFileTool, EditFileTool, MarkReadTool,
    GlobTool, GrepTool,
)


class ReviewDraftTool(Tool):
    """generate-note 内部审稿：校验草稿在最终路径存在，嵌套运行 reviewer 子 agent。

    单目标硬编码 reviewer（权限最小化：被攻陷也只能跑这一个目标，无通用递归）。
    A-ii（2026-08-06 修复）：草稿由 write_file 直接落盘到最终路径（vault note），
    本工具只传 draft_path——不再把整篇草稿塞进工具参数（巨参 draft_text 是 LLM
    跳过审稿的触发点 P5），也不再落盘 scratch 临时文件。
    """

    name = "review_draft"
    description = ("提交笔记草稿给 reviewer 审稿，返回审稿意见。"
                   "草稿已由 write_file 落盘到最终路径（vault note），此处传 draft_path 路径。")
    parameters = {
        "type": "object",
        "properties": {
            # A-ii：草稿已由 write_file 落盘到最终路径，这里只传路径（不再要求把整篇
            # 草稿塞进工具参数——2026-08-06 修复：巨参是 LLM 跳过审稿的触发点 P5）。
            "draft_path": {"type": "string", "format": "path",
                           "description": "笔记文件绝对路径（草稿 v1，write_file 已落盘）"},
            "pdf_path": {"type": "string", "format": "path",
                         "description": "主论文 PDF 绝对路径（供对照原文）"},
            "requirements": {"type": "string",
                             "description": "用户对笔记的自由文本要求（篇幅/语言/侧重/深度等）；"
                                            "无要求则不传，审查跳过要求符合度维度"},
        },
        "required": ["draft_path", "pdf_path"],
    }
    risk_level = "medium"                  # 触发子 agent；不高于 WriteFileTool
    #: 单轮审稿硬超时（默认 120s）：防单轮挂死吃光父预算（D3）。
    #: 超时 → 返回超时消息，generate-note 依现有草稿继续（降级不中断，D10 哲学）。
    #: 类属性（实例可覆盖，测试用极小值）；config 注入留后续。
    review_timeout = 120
    requires_confirm = False               # A-ii：write_file(草稿 v1 落盘，审稿前) 与 edit_file(循环内修订) 才是用户门
    side_effects = ["write_file"]          # 审稿间接触发写（草稿 write_file / 修订 edit_file）
    allowed_roots = ["pdf", "note"]        # pdf_path 走 pdf 根、draft_path 走 note 根
    needs_parent = True                    # 触发 Agent.__init__ opt-in 注入

    def execute(self, draft_path: str, pdf_path: str, requirements: str = "") -> ToolResult:
        parent = getattr(self, "_parent", None)
        if parent is None:
            # 不用 assert：python -O 下断言被剥离，而 parent 缺失是安全敏感路径
            # （无父引用则无法构造子 agent，直接 fail-fast 报错）
            raise RuntimeError("ReviewDraftTool 必须由 Agent 注入 parent")
        cfg = getattr(self, "_config", None)
        if cfg is None:
            # 强制 make_tools 构造：allowed_paths（allowed_roots 的绝对路径解析）由
            # make_tools 注入，绕过它直接实例化会缺安全边界（format="path" 参数无白名单）
            raise RuntimeError("ReviewDraftTool 必须经 make_tools 构造（注入 _config）")
        # 草稿已在最终路径（A-ii）：校验存在，不存在给 LLM 可行动报错（先 write_file 再审稿）
        p = Path(draft_path)
        if not p.exists():
            return ToolResult(text=f"草稿文件不存在: {draft_path}——请先 write_file 落盘草稿再 review_draft")
        # 子 agent：继承父的 llm/registry/security_middleware/session_id
        # （审计链延续：子有自己的 trace_id，靠共享 session_id 与父聚合）
        # 刻意不传 confirm_callback：子默认 fail-safe 的 _default_confirm（始终拒绝）。
        # reviewer 笔记审查模式无 requires_confirm 工具，不会触发确认；将来若有高风险工具，
        # 子侧拒绝而非继承父的自动确认才是正确语义——确认是用户与最外层入口之间的门，
        # 不应被嵌套 agent 透传（spec §4.2）。
        child = Agent(llm=parent.llm, agent_registry=parent.agent_registry,
                      agent_type="reviewer",
                      security_middleware=parent.security_middleware,
                      session_id=parent.session_id)
        task = f"审阅草稿文件 {draft_path}，对照原文 {pdf_path}"
        if requirements:
            # 用户要求 → 拼进子任务文本：reviewer 笔记审查模式从任务文本读取用户要求
            # 并以 requirements 维度审查笔记是否符合。无要求则不拼——向后兼容且跳过该维度。
            task += f"。用户要求：{requirements}"
        try:
            # execute 跑在 to_thread worker 线程（无 running loop）→ asyncio.run 安全新建 loop
            # 嵌套审稿加 wait_for：单轮审稿有时间盒，不会无限挂起拖垮整个
            # generate-note（Supervisor 对 generate-note 的总预算之上再加一层
            # per-round 保护）。超时降级：草稿保持现状，父 LLM 依据现有内容决定。
            text = asyncio.run(asyncio.wait_for(
                child.run(task), timeout=self.review_timeout))
            return ToolResult(text=text)
        except asyncio.TimeoutError:
            return ToolResult(text="审稿子 agent 超时，草稿保持现状，请依据现有内容决定是否定稿")
        except MaxTurnsExceeded as e:
            # 子 agent 超轮 → 转错误文本给父 LLM 决定下一步（不向上抛）
            return ToolResult(text=f"审稿子 agent 超轮: {e}")
        # 无 finally：A-ii 不再落盘 scratch（草稿在最终路径），无临时文件需清理


# 完整 8 工具：审稿桥（review_draft）+ 5 原子工具 + glob/grep（Task 4 定位）。
# review_draft 必须显式在列表内：Task 5 测试的 agent.tools["review_draft"] 依赖此列表，
# 缺失则 KeyError: review_draft。SKILL.md 的审稿循环用 review_draft 传 draft_path 提交草稿，
# edit_file 进循环做修订（A-ii：覆盖写回同一最终路径），同时留给"修改既有笔记"类任务。
# glob/grep：按名定位模板/草稿/PDF（不再盲猜精确路径，P2 路径风暴根因），grep 确认
# edit_file search-replace 的锚点文本。
TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ReadFileTool, WriteFileTool, EditFileTool, MarkReadTool,
    ReviewDraftTool, GlobTool, GrepTool,
])
