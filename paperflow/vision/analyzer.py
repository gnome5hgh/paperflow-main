"""视觉结构化分析：图片 + 图注喂给视觉模型，产出 FigureAnalysis。"""
import base64

from paperflow.core.structured import StructuredOutput, StructuredOutputConfig
from paperflow.vision.schemas import Figure, FigureAnalysis


class FigureAnalyzer:
    """单张图 → 结构化分析。复用 StructuredOutput 三层防御
    （json_mode + 校验重试 + fallback），图片经 content parts 随 prompt 发出。
    """

    def __init__(self, vision_llm, config: StructuredOutputConfig | None = None,
                 telemetry_callback=None):
        """配置视觉 LLM 与分析参数。

        Args:
            vision_llm: 视觉模型客户端（LLMClient(VisionLLMConfig) 或测试假件）。
            config: StructuredOutput 行为参数；None 用默认（json_mode/关思维链/重试 2）。
            telemetry_callback: LLM 调用元数据回调（None 零开销跳过）。
        """
        self._llm = vision_llm
        self._config = config or StructuredOutputConfig(
            json_mode=True, temperature=0.0, disable_thinking=True,
            max_retries=2, max_schema_depth=3)
        self._telemetry_callback = telemetry_callback

    async def analyze(self, figure: Figure) -> FigureAnalysis:
        """分析一张图，返回结构化结果。

        Args:
            figure: FigureExtractor 提取的图表对象。

        Returns:
            FigureAnalysis：结构化分析；视觉输出反复不合法时经 fallback 保底
            （至少 figure.number 有值，insight 等字段留空）。
        """
        prompt = (
            f"这是学术论文的图，图号 Fig.{figure.name or str(figure.number)}。\n"
            f"图注：{figure.caption}\n"
            "请仔细观察图片内容，严格按结构输出分析。"
        )
        data_url = ("data:" + figure.mime + ";base64," +
                    base64.b64encode(figure.image_bytes).decode())
        so = StructuredOutput(self._llm, config=self._config,
                              telemetry_callback=self._telemetry_callback)
        return await so.extract(
            prompt, FigureAnalysis,
            images=[data_url],
            fallback=lambda: FigureAnalysis(number=figure.number, caption=figure.caption),
        )
