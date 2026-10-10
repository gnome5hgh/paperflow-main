"""ViewImageTool：按对象键取回检索到的图表原图，让模型看图并回答。

检索结果里媒体块会带一个**图片对象键**（那列空着的就是普通文本块）。模型看到键之后
调本工具，这里把图取回来直接喂给（多模态的）主模型，返回它对图的回答。

**为什么是工具而不是往上下文里塞图**：图片一旦进了消息列表，之后每一轮都会重发（token
随轮数涨），base64 还会被消息管理器全量落库；包在工具调用里则只在这一次调用内消耗，
用完即散。也因此不需要动工具结果的结构、也不需要动 ReAct 主循环。

失败一律降级为文本（键不存在、对象存储不可用、模型没配），不抛——图是加分项。
"""
import asyncio
import base64

from paperflow.core.llm import LLMClient, Message
from paperflow.core.tool import Tool, ToolResult

#: 不给问题时默认问什么。要的是「这张图画了什么」，不是泛泛的图片描述。
_DEFAULT_QUESTION = "这张图/表展示了什么？说清它的核心内容、关键结论与坐标轴或列名的含义。"


class ViewImageTool(Tool):
    """看图工具：按对象键取图 → 交给多模态模型 → 返回文字回答。

    Attributes:
        name: str，工具名 "view_image"
        description: str，工具描述
        parameters: dict，JSON Schema（image_key / question）
        risk_level: str，"low"
        side_effects: list[str]，["network"]（要把图发给模型）
        output_scan: str，"mark"（图的描述属外部内容）
        needs_parent: bool，True（模型调用归属父 agent 轮次进审计）
        _llm: 多模态模型客户端（可注入；None 时按配置惰性构造）
        _store: 图表原图存取器（可注入；None 时取 RAG 服务的）
    """

    name = "view_image"
    description = ("查看检索回来的图表原图：按检索结果「图片对象键」列给出的键取图，"
                   "让模型看图并回答问题。检索命中媒体块（表/图）时可用它看清图表内容"
                   "——那类块的摘录是空的，内容在图里。")
    parameters = {
        "type": "object",
        "properties": {
            "image_key": {"type": "string",
                          "description": "检索结果「图片对象键」列给出的键"},
            "question": {"type": "string",
                         "description": "想问这张图什么；缺省问它的核心内容与关键结论"},
        },
        "required": ["image_key"],
    }
    risk_level = "low"
    side_effects = ["network"]              # 把图发给模型
    output_scan = "mark"                    # 模型对图内容的描述属外部内容
    #: 需要父 Agent 引用：模型调用归属父 agent 的轮次进审计（见 _telemetry）
    needs_parent = True

    def __init__(self, llm=None, store=None):
        """模型与存取器都可注入（测试传假件）；None 时按配置惰性构造。

        Args:
            llm: 多模态模型客户端；None 时取 RAG 服务配置里的主模型。
            store: 图表原图存取器；None 时取 RAG 服务的存取器。
        """
        self._llm = llm
        self._store = store

    def _telemetry(self):
        """构造模型调用的元数据回调：归属父 agent 的当前轮次进审计。

        直接构造（无父引用，如测试）时返回 None——零开销不接线。

        Returns:
            回调 | None: 接收调用元数据的回调。
        """
        parent = getattr(self, "_parent", None)
        if parent is None:
            return None
        return lambda data: parent._emit_llm_call(
            getattr(parent, "_current_turn", 0), data)

    def execute(self, image_key: str, question: str | None = None) -> ToolResult:
        """取图 → 交模型看图 → 返回回答；任何失败降级为文本。

        Args:
            image_key: 检索结果给出的图片对象键。
            question: 想问什么；缺省问核心内容与关键结论。

        Returns:
            ToolResult：text 为模型对图的回答或降级说明。
        """
        store = self._store
        llm = self._llm
        if store is None or llm is None:
            # 只在真的缺依赖时才去取全局服务（测试两样都注入时就不碰它）
            from paperflow.rag.services.rag_service import get_rag_service
            svc = get_rag_service()
            if store is None:
                store = svc.image_store
            if llm is None:
                llm = LLMClient(svc.config.llm)

        data = store.get(image_key)
        if not data:
            return ToolResult(text=(
                f"取不到这张图（键 {image_key}）：对象不存在，或对象存储不可用。"
                "可继续基于已有文字作答，并如实说明看不到图。"))

        data_url = "data:image/png;base64," + base64.b64encode(data).decode()
        message = Message(role="user", content=[
            {"type": "text", "text": (question or _DEFAULT_QUESTION)},
            {"type": "image_url", "image_url": {"url": data_url}},
        ])
        try:
            # 工具跑在自己的线程里，新建事件循环安全（与图表分析工具同一模式）
            reply = asyncio.run(llm.chat([message],
                                         telemetry_callback=self._telemetry()))
        except Exception as e:
            return ToolResult(text=f"看图失败（{e}）：可基于已有文字作答，并如实说明看不到图。")
        answer = (reply.content or "").strip()
        return ToolResult(text=answer or "模型没有给出内容。")
