"""AnalyzeFiguresTool：提取 PDF 图表并用视觉模型结构化分析。

note-agent 笔记 §5 用它拿「图 + 逐图分析」（embed_dir 把图存进 note 目录返回
Obsidian 嵌入标记）；qa-agent 图表问答用它单图分析。视觉 key 缺失/无图/
调用失败全部降级为文本反馈——笔记流程不被视觉故障打断。二进制 PNG 直接落盘
（atomic_write_bytes），不走 write_file——避免触发 RAG 热索引钩子污染向量库。
"""
import asyncio
import re
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.llm import LLMClient
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.file.atomic import atomic_write_bytes
from paperflow.vision.analyzer import FigureAnalyzer
from paperflow.vision.extractor import FigureExtractor

#: 单次分析图数上限（防超长论文拖死工具；超限截断并如实标注）
MAX_FIGURES = 12


class AnalyzeFiguresTool(Tool):
    """提取并分析 PDF 图表的工具（视觉模型看图，可选把图存进笔记目录）。

    Attributes:
        name: str，工具名 "analyze_figures"
        description: str，工具描述
        parameters: dict，JSON Schema（path/figure/embed_dir）
        risk_level: str，"low"
        root_hints: list[str]，["note", "pdf"]
        output_scan: str，"mark"（图表分析结果属外部内容）
        side_effects: list[str]，["read_file", "write_file"]
        needs_parent: bool，True（视觉调用归属父 agent 轮次进审计）
        _vision_llm: 视觉模型客户端（可注入；None 时按配置惰性构造）
    """
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
    root_hints = ["note", "pdf"]              # 提示:读 PDF + 产图落 note（embed_dir）
    output_scan = "mark"
    side_effects = ["read_file", "write_file"]
    #: 需要父 Agent 引用：视觉调用归属父 agent 的轮次进审计（见 _telemetry）
    needs_parent = True

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

    def _telemetry(self):
        """构造视觉 LLM 调用的元数据回调：归属父 agent 的当前轮次进审计。

        对齐 spawn 的既有接线模式（LLM 调用全审计不变式）：每张图的 GLM-4V 调用
        产出 record_llm_call 元数据，trace/session/agent_type 由父 agent 补全。
        直接构造（无 Agent 注入 _parent，如测试）返回 None——零开销不接线。
        """
        parent = getattr(self, "_parent", None)
        if parent is None:
            return None
        return lambda data: parent._emit_llm_call(
            getattr(parent, "_current_turn", 0), data)

    def effective_target_path(self, args: dict) -> str | None:
        """导出写互斥键：本次真正会写的目标。

        本工具的 path 参数是要读的输入 PDF，不是写目标；唯一会写的是 embed_dir 下的
        图文件，而且不给 embed_dir 时**零写入**（纯分析）。所以键取 embed_dir——若用
        path（输入 PDF）当键，两路并发分析同一篇论文会互相误拒。

        Args:
            args: dict，已解析的工具调用参数

        Returns:
            embed_dir 的路径字符串；未给该参数（本次不落盘）时 None。
        """
        d = args.get("embed_dir")
        return str(d) if isinstance(d, str) and d else None

    def execute(self, path: str, figure: int | None = None,
                embed_dir: str | None = None) -> ToolResult:
        """提取图表 → 视觉分析（并行）→ 可选落盘 → 返回 digest。

        Args:
            path: PDF 绝对路径。
            figure: 指定图号只分析该图；None 分析全部（上限 MAX_FIGURES）。
            embed_dir: 非 None 时把每张图存为 <pdf-stem>-fig<name>.<ext>（name 为图号
                原始串，非整数图号据此不互覆）并返回嵌入标记。

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
            target = str(figure)
            # 按 number 解析值匹配为主（number=0 的非整数图号归为一组）；
            # name 精确匹配为辅——只匹配 name 恰好是整数串的情况，不拆分组。
            # 终审 ruling 正式接受：figure=0 一次匹配全部非整数名图（"3.1"/"III"/"S1"），
            # 因为 int 参数表达不了非整数图号，语义已在 description/参数注释文档化。
            matched = [f for f in figures
                       if str(f.number) == target or f.name == target]
            if not matched:
                known = "、".join(f.name or str(f.number) for f in figures)
                return ToolResult(
                    text=f"PDF 中未找到 Fig.{figure}（已检测到图号：{known}）",
                    summary={"figures": []})
            figures = matched
        truncated = len(figures) > MAX_FIGURES
        if truncated:
            figures = figures[:MAX_FIGURES]

        try:  # 兜底：视觉分析/落盘等任何未料异常降级为文本，绝不抛穿
            analyzer = FigureAnalyzer(llm, telemetry_callback=self._telemetry())
            analyses = asyncio.run(_analyze_all(analyzer, figures))

            pdf_stem = Path(path).stem
            lines: list[str] = []
            embedded: list[dict] = []
            for fig, analysis in zip(figures, analyses):
                if isinstance(analysis, Exception):  # 单图视觉调用失败 → 该图标注跳过，不短路整批
                    lines.append(f"### Fig.{fig.name or fig.number} {fig.caption}")
                    lines.append("- 分析失败：视觉模型调用异常，该图已跳过")
                    lines.append("")
                    continue
                embed_mark = ""
                if embed_dir:
                    embed_mark = _save_figure(fig, Path(embed_dir), pdf_stem)
                lines.append(f"### Fig.{fig.name or fig.number} {fig.caption}")
                lines.append(f"- 核心内容：{analysis.insight}")
                lines.append(f"- 图表类型：{analysis.chart_type}")
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
        except Exception as e:  # 兜底：任何未料异常降级为文本反馈，不抛穿
            return ToolResult(text=f"图表分析失败：{e}", summary={"figures": []})


async def _analyze_all(analyzer: FigureAnalyzer, figures: list) -> list:
    """并行分析多张图。

    asyncio.gather 返回 Future 而非协程，asyncio.run 只接受协程，故经此
    async 包装后再交给 asyncio.run（工具跑在独立线程，新建事件循环安全）。
    return_exceptions=True：单图视觉调用失败（网络断/5xx/限流/key 无效——
    StructuredOutput 只兜 JSON 解析与校验错误，LLM 调用异常会穿出）不短路整批，
    该图异常项随列表返回，由 execute 逐图降级标注。

    Args:
        analyzer: FigureAnalyzer，单图分析器
        figures: list，待分析图表

    Returns:
        与输入顺序一致的分析结果列表；单图异常项以异常对象形式返回（不短路整批）。
    """
    return await asyncio.gather(*[analyzer.analyze(f) for f in figures],
                                return_exceptions=True)


def _save_figure(fig, embed_dir: Path, pdf_stem: str) -> str:
    """把图存到 embed_dir 并返回 Obsidian 嵌入标记。

    文件名以图号原始串 fig.name 为键（非解析出的 int number）——"3.1"/"III"/"S1"
    这类非整数图号的 number 都归 0，按 number 命名会静默互覆成同一文件；name 为空
    时回退 number。文件名做安全化（/、\、空格等替换为 _）。扩展名按 mime 取
    （png→.png / jpeg→.jpg / 其余 .img）；落盘失败不抛——返回嵌入标记即使文件
    没写上也保持流程不断（调用方据文件存在性判断）。

    Args:
        fig: 图表对象（含 name/number/mime/image_bytes）
        embed_dir: Path，图保存目录（按需创建）
        pdf_stem: str，PDF 文件名主干（构成图文件名）

    Returns:
        Obsidian 嵌入标记 "![[<name>]]"；落盘失败不抛，标记照常返回。
    """
    ext = {"image/png": "png", "image/jpeg": "jpg"}.get(fig.mime, "img")
    label = re.sub(r"[/\\\s]+", "_", fig.name or str(fig.number))
    name = f"{pdf_stem}-fig{label}.{ext}"
    try:
        # embed_dir 可能是尚不存在的 figures/ 子目录——按需创建，避免写盘静默失败
        atomic_write_bytes(embed_dir / name, fig.image_bytes)
    except OSError:
        pass
    return f"![[{name}]]"
