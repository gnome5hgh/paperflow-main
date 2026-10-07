"""WriteFileTool：写入或整篇重写文件(filename+dir 便捷入口 / path 精确入口)。

path 可写任意绝对路径(敏感路径黑名单除外,WorkspacePolicyMiddleware 强制)。
filename+dir 便捷入口省略 dir 时落装配注入的默认根(factory default_write_root:
noter→note、researcher→research——代码层防产物错位)。写入语料库(note/pdf)触发
RAG 索引热更新;其余路径写入不进向量库(索引器自滤,见 rag/services/indexer.py)。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.core.security.middleware.workspace import is_denied_path
from paperflow.rag.services.rag_service import get_rag_service
from paperflow.tools.file._constants import NOTE_HINTS
from paperflow.tools.file.atomic import atomic_write


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "写入或整篇重写文件。两种入口二选一：① path=绝对路径（精确控制，敏感路径黑名单外均可写）；"
        "② filename=纯文件名（可选 dir=绝对目录，缺省落本 agent 默认根）。"
        "已存在的文件将被覆盖（小范围修改请用 edit_file 定向替换）；"
        "写入语料库（note/pdf 根内）自动进 RAG 检索，临时文件请显式给 dir。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path",
                     "description": "目标文件绝对路径（与 filename 二选一）"},
            "filename": {"type": "string",
                         "description": "纯文件名（不得含路径分隔符或..；与 path 二选一）"},
            "dir": {"type": "string", "format": "path",
                    "description": "filename 模式的目标目录（缺省=本 agent 默认写根）"},
            "content": {"type": "string", "format": "content", "description": "文件完整新内容"},
        },
        "required": ["content"],
    }
    risk_level = "medium"                      # 写操作；全文覆盖可确认重来 → medium
    requires_confirm = True
    root_hints = NOTE_HINTS
    side_effects = ["write_file"]
    #: 注入本次 run 的状态容器，用于把落盘路径登记进产物账本
    wants_run_state = True

    def _resolve_target(self, path, filename, dir) -> Path:
        """双入口 → 落盘路径的组合规则唯一真相源。

        execute() 与 effective_target_path() 共用，组合规则只写这一份：
        path 模式 → Path(path)；filename 模式 → (dir 或默认写根)/filename。
        入口冲突/缺失、filename 含路径分隔符或 ".."、无默认写根——一律抛
        ValueError（消息即 LLM 面报错文本）。execute 把它转成 ToolResult，
        effective_target_path 把它折叠为 None（该调用必然报错、不落盘，
        确认键/写锁无需作用域）。
        """
        # 双入口互斥校验：path 与 filename 恰好给一个
        if path and filename:
            raise ValueError("path 与 filename 只能二选一，请勿同时提供")
        if not path and not filename:
            raise ValueError("必须提供 path（绝对路径）或 filename（落默认根/指定 dir）")
        if path:
            return Path(path)
        # filename 不是 path 参数、不经中间件校验——穿越与敏感名在此工具内防
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法 filename: {filename!r}——只允许纯文件名，目录请用 dir 参数")
        root = dir or getattr(self, "_default_write_root", None)
        if not root:
            raise ValueError("未指定 dir 且本 agent 未配置默认写根——请显式传 dir（绝对目录）或改用 path（绝对路径）")
        return Path(root) / filename

    def effective_target_path(self, args: dict) -> str | None:
        """导出有效目标路径供会话确认键与写锁键控。

        filename 便捷入口在此导出组合落盘路径：同一文件无论经 path 还是
        filename 入口写，确认键与写锁键一致；不同文件各自确认、互不串锁。
        """
        try:
            return str(self._resolve_target(args.get("path"), args.get("filename"),
                                            args.get("dir")))
        except (ValueError, TypeError):
            return None

    def execute(self, content: str, path: str | None = None,
                filename: str | None = None, dir: str | None = None,
                _run_state=None) -> ToolResult:
        """解析双入口 → 黑名单兜底 → 写盘 → 登记产物 → 索引热更新。

        _run_state 为本次 run 的状态容器（未注入时为 None）：写盘成功后把落盘路径
        登记进产物账本，供后续收尾核对；索引失败不影响登记（登记在写盘之后即已确定）。
        """
        try:
            p = self._resolve_target(path, filename, dir)
        except ValueError as e:
            return ToolResult(text=str(e))
        # filename+dir 模式组合出的路径不经中间件，黑名单在此兜底（防 .env 等敏感名落盘）
        cfg = getattr(self, "_config", None)
        if cfg is not None and is_denied_path(p, cfg.runtime.workspace):
            return ToolResult(text=f"敏感路径受保护，拒绝写入: {p}")
        atomic_write(p, content)
        if _run_state is not None:
            _run_state.artifacts[str(p)] = "write_file"
        note = ""
        try:
            get_rag_service().index_document(str(p))
        except Exception as e:
            # Milvus 是外部服务可能未启动：索引失败只降级为提示，不掩盖已成功的写入
            note = f"（索引失败：{e}）"
        return ToolResult(text=f"已写入 {p}{note}", completion=f"File written: {p}")
