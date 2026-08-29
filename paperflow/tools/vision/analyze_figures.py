"""AnalyzeFiguresTool：提取 PDF 图表并用视觉模型结构化分析。

writer 笔记 §5 用它拿「图 + 逐图分析」（embed_dir 把图存进 note 目录返回
Obsidian 嵌入标记）；qa-agent 图表问答用它单图分析。视觉 key 缺失/无图/
调用失败全部降级为文本反馈——笔记流程不被视觉故障打断。二进制 PNG 直接落盘
（Path.write_bytes），不走 write_file——避免触发 RAG 热索引钩子污染向量库。
"""
import asyncio
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.llm import LLMClient
from paperflow.core.tool import Tool, ToolResult
from paperflow.vision.analyzer import FigureAnalyzer
from paperflow.vision.extractor import FigureExtractor

#: 单次分析图数上限（防超长论文拖死工具；超限截断并如实标注）
MAX_FIGURES = 12


class AnalyzeFiguresTool(Tool):
    name = "analyze_figures"
    description = ("提取并分析 PDF 中的图表：视觉模型看图，返回每张图的分析"
                   "（核心内容/图表类型/新颖之处/适用场景/制作工具推测/配色布局标注）。"
                   "figure 指定图号则只分析该图，否则分析全部（最多 12 张）。"
                   "embed_dir 给出时把图保存到该目录并返回 Obsidian ![[嵌入标记]]。")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "PDF 绝对路径"},
            "figure": {"type": "integer", "description": "只分析指定图号；缺省分析全部"},
            "embed_dir": {"type": "string", "format": "path",
                          "description": "把图保存到该目录并返回 ![[嵌入标记]]"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    allowed_roots = ["note", "pdf"]              # 读 PDF + 写 note 目录
    output_scan = "mark"
    side_effects = ["read_file", "write_file"]

    def __init__(self, vision_llm=None):
        """视觉模型可注入（测试）；None 时按配置惰性构造。

        Args:
            vision_llm: 视觉模型客户端（LLMClient(VisionLLMConfig)）；测试传假件，
                None 时从 config.vision 构造。
        """
        self._vision_llm = vision_llm

    def _get_vision_llm(self):
        """取视觉模型客户端；vision api_key 未配置返回 None（调用方降级）。

        优先用注入实例；否则读注入的 _config，兜底 PaperFlowConfig.from_env()。
        """
        if self._vision_llm is not None:
            return self._vision_llm
        cfg = getattr(self, "_config", None) or PaperFlowConfig.from_env()
        if not cfg.vision.api_key:
            return None
        return LLMClient(cfg.vision)

    def execute(self, path: str, figure: int | None = None,
                embed_dir: str | None = None) -> ToolResult:
        """提取图表 → 视觉分析（并行）→ 可选落盘 → 返回 digest。

        Args:
            path: PDF 绝对路径。
            figure: 指定图号只分析该图；None 分析全部（上限 MAX_FIGURES）。
            embed_dir: 非 None 时把每张图存为 <pdf-stem>-fig<N>.<ext> 并返回嵌入标记。

        Returns:
            ToolResult：text 为逐图分析 digest（含嵌入标记），summary 携带
            figures 结构化列表。任何故障降级为文本，不抛异常。
        """
        llm = self._get_vision_llm()
        if llm is None:
            return ToolResult(
                text="图表分析不可用：未配置视觉模型 key（PAPERFLOW_VISION_API_KEY）。"
                     "§5 图表部分无法生成，请在 .env 配置视觉模型后重试。",
                summary={"figures": []})
        try:
            figures = FigureExtractor().extract(path)
        except Exception as e:  # PDF 打不开/解析异常 → 降级
            return ToolResult(text=f"图表提取失败：{e}", summary={"figures": []})
        if not figures:
            return ToolResult(text="该 PDF 未检测到图表（无图注/无图区）",
                              summary={"figures": []})
        if figure is not None:
            matched = [f for f in figures if f.number == figure]
            if not matched:
                known = "、".join(str(f.number) for f in figures)
                return ToolResult(
                    text=f"PDF 中未找到 Fig.{figure}（已检测到图号：{known}）",
                    summary={"figures": []})
            figures = matched
        truncated = len(figures) > MAX_FIGURES
        if truncated:
            figures = figures[:MAX_FIGURES]

        analyzer = FigureAnalyzer(llm)
        analyses = asyncio.run(_analyze_all(analyzer, figures))

        pdf_stem = Path(path).stem
        lines: list[str] = []
        embedded: list[dict] = []
        for fig, analysis in zip(figures, analyses):
            embed_mark = ""
            if embed_dir:
                embed_mark = _save_figure(fig, Path(embed_dir), pdf_stem)
            lines.append(f"### Fig.{fig.number} {fig.caption}")
            lines.append(f"- 核心内容：{analysis.insight}")
            lines.append(f"- 图表类型：{analysis.chart_type}；展示目的：{analysis.insight}")
            lines.append(f"- 新颖之处：{analysis.notable}")
            lines.append(f"- 适用场景：{analysis.applicable_scenarios}")
            lines.append(f"- 制作工具推测：{analysis.tool_guess}")
            lines.append(f"- 配色：{analysis.color_scheme}；布局：{analysis.layout_tips}；"
                         f"字体标注：{analysis.font_annotation}")
            if embed_mark:
                lines.append(f"- 图：{embed_mark}")
            lines.append("")
            embedded.append({**analysis.model_dump(), "embed": embed_mark})
        if truncated:
            lines.append(f"（仅展示前 {MAX_FIGURES} 张，其余省略）")
        return ToolResult(text="\n".join(lines), summary={"figures": embedded})


async def _analyze_all(analyzer: FigureAnalyzer, figures: list) -> list:
    """并行分析多张图。

    asyncio.gather 返回 Future 而非协程，asyncio.run 只接受协程，故经此
    async 包装后再交给 asyncio.run（工具跑在独立线程，新建事件循环安全）。
    """
    return await asyncio.gather(*[analyzer.analyze(f) for f in figures])


def _save_figure(fig, embed_dir: Path, pdf_stem: str) -> str:
    """把图存到 embed_dir 并返回 Obsidian 嵌入标记。

    扩展名按 mime 取（png→.png / jpeg→.jpg / 其余 .img）；落盘失败不抛——
    返回嵌入标记即使文件没写上也保持流程不断（调用方据文件存在性判断）。
    """
    ext = {"image/png": "png", "image/jpeg": "jpg"}.get(fig.mime, "img")
    name = f"{pdf_stem}-fig{fig.number}.{ext}"
    try:
        (embed_dir / name).write_bytes(fig.image_bytes)
    except OSError:
        pass
    return f"![[{name}]]"
