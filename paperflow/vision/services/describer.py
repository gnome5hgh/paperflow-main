"""单张区域图 → **检索用描述**：只转写「图上有什么」，不做解读。

与同目录 ``analyzer.FigureAnalyzer`` 的分工是刻意的，别互相替换：

- ``FigureAnalyzer`` 产出 ``FigureAnalysis``（``insight`` / ``notable`` / ``applicable_scenarios`` /
  ``tool_guess`` / ``color_scheme`` / ``layout_tips`` / ``font_annotation``）——那是给**写笔记做图表
  点评**用的**解读式**结果。
- 本模块产出的是**一段纯文本**，写进媒体块的 ``text`` 作为**检索信号**。它要的是图里**字面**存在的
  内容（标题、坐标轴、图例、数值、标注），因为用户的问题是拿这些词去匹配的；``tool_guess`` /
  ``color_scheme`` 那类字段进索引只会变成噪声。

因此 prompt 的三条取向（照 RAGFlow 的图注描述 prompt 设计）：**按图的性质分两模式输出**、
**逐字转写可见文本**、**明确禁止推断意图或含义**。写进索引的内容必须能在图上指出来——这是把幻觉
挡在索引之外的护栏，漏掉它等于往库里注入「图上并不存在的内容」。
"""
from __future__ import annotations

import asyncio
import base64
import threading

#: 描述 prompt。两模式（含可枚举数据单元 → 结构化字段；否则散文）+ 只转写不解释。
_DESCRIBE_PROMPT = """你是学术论文插图的信息提取员。请**只**输出图上可见的内容。

第一步，判断图的性质：

- 若图里含**可枚举的数据单元**（表格的行列、柱状图的柱、折线图的数据点、饼图的扇区等彼此可分、
  可比较或可累加的元素），按下面五个字段逐行输出，字段名照写：
  类型：
  标题：
  坐标轴 / 图例 / 标签：
  数据点：
  图注 / 标注：
- 否则，用连续句子按自上而下、自左而右的稳定顺序描述图中可见的区域与元素。

硬性要求（违反即视为无效输出）：

1. **逐字转写**图上出现的一切文字（标题、轴标签、图例、数值、注释），不改写、不概括、不翻译；
2. 只写图上**看得见**的东西，**不要**推断图的意图、功能、流程或含义；
3. 图上没有出现的说法不要用来描述这张图（例如「流程图」「示意图」「训练过程」），除非图里自己写了；
4. 不要写「这张图展示了……」「该图说明……」这类前言或评论，直接从内容写起。"""


class FigureDescriber:
    """把一张区域图（``Figure``）描述成一段检索用文本。

    ``Figure.image_bytes`` 需已就绪（由 ``FigureExtractor.render`` 渲染）。视觉模型不可用
    （未配 api_key）时由调用方走降级——这里用 :attr:`available` 显式暴露可用性，不抛异常。

    Attributes:
        _config: PaperFlowConfig（取 ``vision`` 段建客户端）。
        _llm: 视觉模型客户端；可注入（测试传假件）。
    """

    def __init__(self, config=None, llm=None):
        """Args:
            config: PaperFlowConfig；None 时从环境装载（便于直接手跑）。
            llm: 视觉模型客户端；None 时按 ``config.vision`` 惰性构造。
        """
        if config is None:
            from paperflow.config import PaperFlowConfig

            config = PaperFlowConfig.from_env()
        self._config = config
        self._llm = llm

    @property
    def available(self) -> bool:
        """视觉模型是否可用（未配 api_key 即不可用——索引照常，只是不带描述）。"""
        if self._llm is not None:
            return True
        return bool((getattr(self._config.vision, "api_key", "") or "").strip())

    def describe(self, figure) -> str:
        """描述一张图，返回纯文本；两次都拿不到就返回空串（调用方保留原形态）。

        同步入口：索引器是同步代码，而 ``LLMClient.chat`` 是异步，中间的桥见 :func:`_run_sync`。

        **空结果重试一次**：视觉模型是推理模型，思维链长度每次都会变，偶尔一次能把整个输出预算
        跑光——响应里 ``finish_reason=length`` 且 ``content`` 是空串（同一张图两次结果可以一次空
        一次正常，实测过）。重画一次思维链常常就出稿，所以空结果值得再来一次；只重试一次是为了
        给「这张图真的描述不完」的情况兜底，不至于无限烧调用。

        Args:
            figure: 已渲染的 ``Figure``（用 ``image_bytes`` / ``mime`` / ``caption``）。

        Returns:
            str: 模型输出的描述；无字节、模型不可用或两次都没产出时为空串。
        """
        if not self.available:
            return ""
        data = getattr(figure, "image_bytes", b"") or b""
        if not data:
            return ""
        mime = getattr(figure, "mime", "") or "image/png"
        data_url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        for _attempt in range(2):
            try:
                text = _run_sync(self._chat(data_url))
            except Exception:                 # noqa: BLE001 —— 单张失败不该影响索引
                text = ""
            if text:
                return text
        return ""

    async def _chat(self, data_url: str) -> str:
        """发起一次多模态调用，返回描述文本（可能为空——见 :meth:`describe`）。"""
        from paperflow.core.llm import Message

        llm = self._llm
        if llm is None:
            from paperflow.core.llm import LLMClient

            llm = LLMClient(self._config.vision)
            self._llm = llm
        message = Message(role="user", content=[
            {"type": "text", "text": _DESCRIBE_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ])
        reply = await llm.chat([message], temperature=0.0)
        return str(reply.content or "").strip()


def _run_sync(coro):
    """在同步上下文里跑一个协程。

    索引器是同步代码，但会被两种调用路径进来：工作线程（无事件循环）与可能已在事件循环里的
    异步路径。前者直接 ``asyncio.run``，后者丢到独立线程用新循环跑——否则会撞上
    「asyncio.run() cannot be called from a running event loop」。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def _worker():
        box["value"] = asyncio.run(coro)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    return box.get("value")
