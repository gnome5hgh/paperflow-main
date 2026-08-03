# agents/generate-note/tools.py（6 工具完整装配：5 原子工具 + ReviewDraftTool）
"""generate-note 的工具装配。

5 个原子工具（read_file/read_pdf/write_file/edit_file/mark_read）复用
paperflow/tools/file.py 的集中式安全边界（WorkspacePolicy 白名单、风险语义）；
ReviewDraftTool 是"集中式原子工具"约定的刻意例外——定义在 agent 目录而非
paperflow/tools/：单消费者（仅 generate-note）、需 parent 注入（needs_parent）。
它还是 Layer 4 spawn 的种子：届时 SpawnSubAgentTool 同样落 agents/supervisor/tools.py，
内部"实例化子 agent → run"原样保留，只换 wrapper（SubAgentResult + 参数化 agent 名 + allowed_spawns）。
"""
import asyncio
from pathlib import Path
from uuid import uuid4

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.factory import make_tools
from paperflow.tools.file import (
    ReadFileTool, ReadPdfTool, WriteFileTool, EditFileTool, MarkReadTool,
)


class ReviewDraftTool(Tool):
    """generate-note 内部审稿：把草稿落盘到 scratch，嵌套运行 review-note 子 agent。

    单目标硬编码 review-note（权限最小化：被攻陷也只能跑这一个目标，无通用递归）。
    draft_text 标 format="content"：关闭唯一一条未扫描的不可信输入通道
    （draft 源自外部 PDF，可能携带注入 payload → 子 agent 上下文）。
    """

    name = "review_draft"
    description = ("提交笔记草稿给 review-note 审稿，返回审稿意见。"
                   "草稿在上下文中生成，本工具负责落盘桥接与清理。")
    parameters = {
        "type": "object",
        "properties": {
            "draft_text": {"type": "string", "format": "content",
                           "description": "笔记草稿全文（Markdown）"},
            "pdf_path": {"type": "string", "format": "path",
                         "description": "主论文 PDF 绝对路径（供对照原文）"},
        },
        "required": ["draft_text", "pdf_path"],
    }
    risk_level = "medium"                  # 瞬态写盘 + 触发子 agent；不高于 WriteFileTool
    requires_confirm = False               # 审稿循环最多 3 轮，最终 WriteFileTool 才是用户门
    side_effects = ["write_file"]          # 写 scratch 临时文件
    allowed_roots = ["pdf"]                # pdf_path 父级门控（子 agent ReadPdf 再门控）
    needs_parent = True                    # 触发 Agent.__init__ opt-in 注入

    def execute(self, draft_text: str, pdf_path: str) -> ToolResult:
        parent = getattr(self, "_parent", None)
        assert parent is not None, "ReviewDraftTool 必须由 Agent 注入 parent"
        cfg = getattr(self, "_config", None)
        assert cfg is not None, "ReviewDraftTool 必须经 make_tools 构造（注入 _config）"
        # scratch 派生基准 = config.workspace（make_tools 注入，避免经 get_rag_service 间接取）
        scratch_dir = Path(cfg.workspace) / "tmp"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        draft_path = scratch_dir / f"review_{parent.session_id}_{uuid4().hex[:8]}.md"
        draft_path.write_text(draft_text, encoding="utf-8")
        try:
            # 子 agent：继承父的 llm/registry/security_middleware/session_id
            # （审计链延续：子有自己的 trace_id，靠共享 session_id 与父聚合）
            child = Agent(llm=parent.llm, agent_registry=parent.agent_registry,
                          agent_type="review-note",
                          security_middleware=parent.security_middleware,
                          session_id=parent.session_id)
            task = f"审阅草稿文件 {draft_path}，对照原文 {pdf_path}"
            # execute 跑在 to_thread worker 线程（无 running loop）→ asyncio.run 安全新建 loop
            text = asyncio.run(child.run(task))
            return ToolResult(text=text)
        except MaxTurnsExceeded as e:
            # 子 agent 超轮 → 转错误文本给父 LLM 决定下一步（不向上抛）
            return ToolResult(text=f"审稿子 agent 超轮: {e}")
        finally:
            # 瞬态清理：不污染 vault、不落 Git（GitStore 只跟踪 memory/*.md）
            draft_path.unlink(missing_ok=True)


# 完整 6 工具：审稿桥（review_draft）+ 5 原子工具。
# review_draft 必须显式在列表内：Task 5 测试的 agent.tools["review_draft"] 依赖此列表，
# 缺失则 KeyError: review_draft。SKILL.md 的审稿循环用 review_draft 提交草稿，
# edit_file 不进循环（修订只在上下文进行），但仍在工具面——留给"修改既有笔记"类任务。
TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ReadFileTool, WriteFileTool, EditFileTool, MarkReadTool, ReviewDraftTool,
])
