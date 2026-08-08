# paperflow/tools/submit_download_review.py
"""SubmitDownloadReviewTool：汇总下载审查裁决（reviewer 下载审查模式返回）。

与 SubmitReviewTool 同款校验哲学：decision/verdict 枚举 + 一致性 + 可行动理由，
非法输入返回可行动报错文本，不静默吞（让 reviewer 的 LLM 修正后重试）。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.review._validate import VERDICTS, enum_check


class SubmitDownloadReviewTool(Tool):
    name = "submit_download_review"
    description = ("汇总对候选论文清单的下载审查裁决（reviewer 下载审查模式返回）。"
                   "pass = 存在可下载/推荐项；fail = 无任何合格项。"
                   "每条 fail 必须带 reasons[]（未通过原因，可含等级/年份/相关性/可下载性）。")
    parameters = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"],
                        "description": "pass = 存在可下载/推荐项；fail = 无任何合格项"},
            "items": {"type": "array", "items": {"type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "venue_rank": {"type": "object", "description": "lookup_venue_rank 返回的等级"},
                    "decision": {"type": "string", "enum": ["pass", "fail"]},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                    "source_link": {"type": "string"},
                },
                "required": ["title", "decision", "reasons", "source_link"]}},
        },
        "required": ["verdict", "items"],
    }
    risk_level = "low"                     # 只读格式化，无副作用（同 SubmitReviewTool）

    def execute(self, verdict: str, items: list) -> ToolResult:
        # ① verdict 枚举校验（enum_check 共享，同 submit_review）
        bad = enum_check(verdict, VERDICTS, "verdict")
        if bad:
            return ToolResult(text=bad)
        # ② 逐条目校验：decision 枚举 + fail 必须带原因 + 必需字段非空。
        #    顺序上先报枚举错再报缺字段——枚举错说明 LLM 理解偏差，缺字段说明格式偏差，
        #    前者更根本，应优先暴露让 LLM 修正。
        for item in items:
            bad = enum_check(item.get("decision"), VERDICTS, "decision")
            if bad:
                return ToolResult(text=bad)
            if item["decision"] == "fail" and not item.get("reasons"):
                return ToolResult(text=f"条目 '{item.get('title')}' decision=fail 但 reasons 为空——每条 fail 必须有原因")
            missing = [k for k in ("title", "source_link") if not item.get(k)]
            if missing:
                return ToolResult(text=f"条目缺少字段: {', '.join(missing)}")
        # ③ verdict 与 items 一致性——按「pass 项存在性」判定（而非旧语义的「fail 项」）：
        #    - verdict=pass 但无任何 pass 条目 → 自相矛盾：pass 语义=存在可下载/推荐项，
        #      一个 pass 都没有却报 pass，属过宽放行（LLM 把"没有合格项"误报成 pass）。
        #    - verdict=fail 但存在 pass 条目 → 自相矛盾（fail 语义=无任何合格项，
        #      输出"审查裁决：fail"却带 [PASS] 行会误导后续门禁）。
        #    - verdict=pass + 混合列表（如 2 pass + 7 fail）→ **合法**：pass 语义只要求
        #      存在可下载/推荐项，剩余 fail 项只是"不值得下载的候选"。旧校验按 fail 项
        #      拦截导致真实审查产出被误拒、逼 reviewer 试错（2026-08-08 冒烟实测）。
        #    - verdict=pass + 空 items → 上面单独拦截（更明确的报错文案，比"无 pass 条目"
        #      更直接）。
        #    注意：verdict=fail + 空 items 必须保持合法（fail = 无合格项，空清单正是
        #    "无任何合格项"的极端情况），any() 对空列表天然返回 False，不拦截。
        if verdict == "pass" and not items:
            return ToolResult(text="verdict=pass 但 items 为空——pass 语义是存在可下载/推荐项，空清单应报 fail")
        if verdict == "pass" and not any(i.get("decision") == "pass" for i in items):
            return ToolResult(text="verdict=pass 但无任何 pass 条目——pass 语义是存在可下载/推荐项，应至少有一个 pass（verdict 与 items 不一致）")
        if verdict == "fail" and any(i.get("decision") == "pass" for i in items):
            return ToolResult(text="verdict=fail 但存在 pass 条目——verdict 与 items 不一致（fail = 无任何合格项）")
        # ④ 格式化：verdict 行 + 每条目 PASS/FAIL 标签 + 原因 + 来源链接（writer 确定性可读）
        lines = [f"审查裁决：{verdict}"]
        for item in items:
            tag = "PASS" if item["decision"] == "pass" else "FAIL"
            reason = "；".join(item.get("reasons") or [])
            lines.append(f"- [{tag}] {item['title']} | {reason} | {item.get('source_link')}")
        return ToolResult(text="\n".join(lines))
