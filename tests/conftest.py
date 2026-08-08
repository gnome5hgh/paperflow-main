# tests/conftest.py
"""
pytest 共享 fixture 和测试辅助工具。

``MockEchoTool`` 是 EchoTool 的副本，用于在测试中不依赖 tests/_demo/ 目录
即可验证 Tool 执行和 Agent._exec_tool 的路由逻辑。
"""

from paperflow.core.tool import Tool, ToolResult


class MockEchoTool(Tool):
    """
    测试用 EchoTool，与 tests/_demo/tools.py 的 EchoTool 行为完全一致。

    定义在 conftest.py 中以便所有测试文件共享，避免重复定义。
    """

    #: 工具名称，与真实 EchoTool 相同
    name = "echo"

    #: 工具描述
    description = "Echo back the input message"

    #: JSON Schema 参数定义，与真实 EchoTool 相同
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message to echo"}
        },
        "required": ["message"],
    }

    def execute(self, message: str) -> ToolResult:
        """
        回显消息，与真实 EchoTool 行为一致。

        :param message: 输入消息
        :returns: ToolResult(text="Echo: <message>")
        """
        return ToolResult(text=f"Echo: {message}")


# ─── Layer 3 共享测试基建（Task 4 起，Task 5-9 的 agent 测试共用）───

import pytest
from pathlib import Path

from paperflow.core.agent import Agent
from paperflow.core.llm import Message
from paperflow.core.agent_registry import AgentRegistry
from paperflow.config import PaperFlowConfig


def make_mock_llm(responses: list[Message]):
    """mock LLM：chat() 按顺序 pop(0) 返回预设 Message（与 test_agent.py 同款，供 agent 测试共享）。"""
    from unittest.mock import MagicMock
    mock = MagicMock()

    async def chat(messages, tools=None, tool_choice="auto"):
        return responses.pop(0)

    mock.chat = chat
    mock.model = "mock"
    return mock


class StubPdfParser:
    """假 PDF 解析器：跳过 GROBID 探测与真实 PyMuPDF，返回固定 sections。"""
    def parse_pdf(self, path):
        from paperflow.rag.grobid_client import ParsedDoc
        return ParsedDoc(
            sections=[("Abstract", "Abstract text."), ("Methods", "Methods text.")],
            tables=[], figures=[],
        )


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """tmp 隔离环境：patch from_env + 三个模块的 get_rag_service。

    patch 顺序钉死：必须在 AgentRegistry 构造之前（_import_tools 的 re-exec
    发生在 _discover 构造时，读 patched from_env 得到 tmp 配置）。"""
    cfg = PaperFlowConfig(
        workspace=str(tmp_path / "ws"),
        vault_note_dir=str(tmp_path / "vault" / "note"),
        vault_pdf_dir=str(tmp_path / "vault" / "pdf"),
        chroma_path=str(tmp_path / "chroma"),
    )
    for sub in ("ws", "vault/note", "vault/pdf"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(PaperFlowConfig, "from_env",
                        classmethod(lambda cls, config_path=None: cfg))

    from paperflow.rag.service import RAGService
    from paperflow.rag.embedder import FakeEmbedder
    svc = RAGService(cfg)
    svc._embedder = FakeEmbedder()          # 假嵌入，避免 2GB 模型下载
    svc._grobid_available = False           # 跳过 GROBID 探测（网络）
    svc._pymupdf_parser = StubPdfParser()   # 假 PDF 解析
    # 绑定 get_rag_service 的模块命名空间都要 patch（工具在模块顶部 import 绑定）：
    # 6 个工具模块（拆分自 file.py/search.py，一工具一文件）+ rag.retriever
    for mod in ("file.read_pdf", "file.write_file", "file.edit_file", "file.format_check",
                "search.fetch_pdf"):
        monkeypatch.setattr(f"paperflow.tools.{mod}.get_rag_service", lambda: svc)
    monkeypatch.setattr("paperflow.rag.retriever.get_rag_service", lambda: svc)
    return cfg, svc


@pytest.fixture
def agent_registry(agent_env):
    """真实 AgentRegistry（扫描 agents/），每个测试新建（patch 先行）。"""
    agents_dir = str(Path(__file__).resolve().parents[1] / "agents")
    return AgentRegistry(agents_dir)


def _tc(name, args, cid="c1"):
    """构造一个 assistant tool_call Message（args 为参数 dict）。"""
    import json
    return Message(role="assistant", content=None, tool_calls=[{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }])


async def _accept_confirm(cr):
    """测试/smoke 用 confirm 回调：自动接受（happy path 验主链路）。

    确认流本身（拒绝路径 / 会话级一次确认）由 Layer 1 测试覆盖；
    这里只保证 agent 测试接真实安全链时 write 能落盘。"""
    return True


def make_agent(agent_registry, agent_type, llm, cfg, confirm=True):
    """构造带真实安全中间件链的 Agent（对齐 __main__.py），audit 落 tmp。

    中间件：① Audit → ② WorkspacePolicy → ③ SecurityScan → ④ PolicyEngine。
    必须传 confirm：WriteFileTool requires_confirm=True（spec §4.1 定稿是用户门），
    无 confirm_callback 时 PolicyEngine 抛 ConfirmRequired → _default_confirm 拒绝
    → 笔记不落盘（smoke 的"真写笔记"副作用也会失效）。"""
    from paperflow.core.security import (
        AuditMiddleware, WorkspacePolicyMiddleware,
        SecurityScanMiddleware, PolicyEngineMiddleware,
    )
    middlewares = [
        AuditMiddleware(audit_dir=str(Path(cfg.workspace) / "audit")),
        WorkspacePolicyMiddleware(workspace=cfg.workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk=cfg.max_risk),
    ]
    return Agent(llm=llm, agent_registry=agent_registry, agent_type=agent_type,
                 security_middleware=middlewares,
                 confirm_callback=_accept_confirm if confirm else None)
