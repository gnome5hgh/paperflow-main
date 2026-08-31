"""视觉结构化分析：图片 + 图注喂给视觉模型，产出 FigureAnalysis。"""

import base64

from paperflow.core.structured import StructuredOutput, StructuredOutputConfig
from paperflow.vision.schemas import Figure, FigureAnalysis


class FigureAnalyzer:
    """单张图 → 结构化分析。

    复用 StructuredOutput 三层防御机制（json_mode + 校验重试 + fallback），
    将图片转为 data URL 随 prompt 一起发送给视觉模型，并确保最终始终返回
    一个合法的 FigureAnalysis 对象（即使模型输出格式错误）。

    Attributes:
        _llm: 视觉模型客户端（LLMClient 或测试替身）。
        _config: StructuredOutput 行为参数。
        _telemetry_callback: LLM 调用元数据回调（可选）。
    """

    def __init__(self, vision_llm, config: StructuredOutputConfig | None = None,
                 telemetry_callback=None):
        """配置视觉 LLM 与分析参数。

        Args:
            vision_llm: 视觉模型客户端（LLMClient(VisionLLMConfig) 或测试假件）。
            config: StructuredOutput 行为参数；None 用默认值：
                - json_mode=True: 强制模型输出 JSON。
                - temperature=0.0: 确定性输出，减少随机变化。
                - disable_thinking=True: 关闭推理链，仅输出最终答案。
                - max_retries=2: 校验失败最多重试 2 次。
                - max_schema_depth=3: 用于防止递归过深。
            telemetry_callback: LLM 调用元数据回调，用于日志/追踪；None 则零开销跳过。
        """
        self._llm = vision_llm
        self._config = config or StructuredOutputConfig(
            json_mode=True, temperature=0.0, disable_thinking=True,
            max_retries=2, max_schema_depth=3)
        self._telemetry_callback = telemetry_callback

    async def analyze(self, figure: Figure) -> FigureAnalysis:
        """分析一张图，返回结构化结果。

        执行流程：
            1. 构建提示词，明确告知图号（优先使用 figure.name，否则用 figure.number）。
            2. 将图片字节编码为 base64 data URL（格式：data:{mime};base64,{bytes}）。
            3. 通过 StructuredOutput 向视觉模型发起请求，要求输出 FigureAnalysis 格式。
            4. 若模型输出不符合 Pydantic 校验，自动重试（最多 config.max_retries 次）。
            5. 若仍失败，执行 fallback 闭包，返回一个至少包含图号和图注的保底对象。

        Args:
            figure: FigureExtractor 提取的图表对象（包含图像字节、MIME、图注等）。

        Returns:
            FigureAnalysis：结构化分析结果；极端情况下（多次失败）经 fallback 保底，
            至少 figure.number 和 caption 有值，其余字段均为空字符串。
        """
        # 构建提示词：明确给出图号（优先使用原始 name，若为空则回退到 number）和图注内容
        prompt = (
            f"这是学术论文的图，图号 Fig.{figure.name or str(figure.number)}。\n"
            f"图注：{figure.caption}\n"
            "请仔细观察图片内容，严格按结构输出分析。"
        )

        # 将图像字节转为标准 data URL（支持多模态输入）
        data_url = ("data:" + figure.mime + ";base64," +
                    base64.b64encode(figure.image_bytes).decode())

        # 复用 StructuredOutput 完成提取（含校验重试和 fallback）
        so = StructuredOutput(self._llm, config=self._config,
                              telemetry_callback=self._telemetry_callback)

        # fallback 闭包：当模型多次输出非法结构时调用，确保总能返回合法对象。
        # 捕获 figure.number 和 figure.caption，保证业务关键信息不丢失。
        return await so.extract(
            prompt, FigureAnalysis,
            images=[data_url],
            fallback=lambda: FigureAnalysis(number=figure.number, caption=figure.caption),
        )