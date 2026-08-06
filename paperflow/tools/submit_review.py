# paperflow/tools/submit_review.py
"""SubmitReviewTool：汇总对一篇笔记的审查裁决（review-note 返回）。

从 SuggestEditTool 升级（2026-08-06 review-note rework）：无类型建议列表 →
结构化裁决（verdict + issues[]，枚举校验）。execute 不读文件内容（只把提交的
字段格式化），放开 scratch 根零安全影响——安全边界与 suggest_edit 一致。

校验哲学对齐 edit_file 的 miss/multi 报错：非法输入返回可行动报错文本，
不静默吞（让 review-note 的 LLM 修正后重试）。
"""
from paperflow.core.tool import Tool, ToolResult

#: severity 合法值（blocking 必须修才能通过；major 应修；minor 可忽略）
SEVERITIES = ("blocking", "major", "minor")
#: dimension 合法值（5 审查维度，2026-08-06 rework 定稿；写作质量维度砍掉）
DIMENSIONS = ("requirements", "faithfulness", "consistency", "completeness", "structure")


class SubmitReviewTool(Tool):
    name = "submit_review"
    description = ("汇总对一篇笔记的审查裁决（review-note 返回）。"
                   "verdict=pass 当且仅当无 blocking 意见；每条 issue 必须可执行（location + action）。")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "笔记绝对路径"},
            "verdict": {"type": "string", "enum": ["pass", "fail"],
                        "description": "pass = 无 blocking 意见；fail = 存在 blocking 意见"},
            "issues": {"type": "array", "items": {"type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocking", "major", "minor"]},
                    "dimension": {"type": "string", "enum": ["requirements", "faithfulness",
                                                             "consistency", "completeness", "structure"]},
                    "location": {"type": "string", "description": "位置（章节/句）"},
                    "action": {"type": "string", "description": "具体修改动作"},
                },
                "required": ["severity", "dimension", "location", "action"]}},
        },
        "required": ["path", "verdict", "issues"],
    }
    risk_level = "low"                     # 只读格式化，无副作用
    # 审稿流目标是 scratch/note 草稿路径；execute 不读文件内容（只格式化提交字段），
    # 放开 scratch 根零安全影响（与 SuggestEditTool 同款，防真实 WorkspacePolicy 拦截）。
    allowed_roots = ["note", "scratch"]

    def execute(self, path: str, verdict: str, issues: list) -> ToolResult:
        # ① verdict 枚举校验
        if verdict not in ("pass", "fail"):
            return ToolResult(text=f"verdict 非法: {verdict}，应为 pass 或 fail")
        # ② 逐 issue 校验：severity/dimension 枚举 + location/action 非空
        for issue in issues:
            sev = issue.get("severity")
            if sev not in SEVERITIES:
                return ToolResult(text=f"severity 非法: {sev}，应为 {', '.join(SEVERITIES)}")
            dim = issue.get("dimension")
            if dim not in DIMENSIONS:
                return ToolResult(text=f"dimension 非法: {dim}，应为 {', '.join(DIMENSIONS)}")
            missing = [k for k in ("location", "action") if not issue.get(k)]
            if missing:
                return ToolResult(text=f"issue 缺少字段: {', '.join(missing)}——每条意见必须有 location + action")
        # ③ verdict 与 issues 一致性（pass 语义被破坏则报错，给 LLM 可行动指引）
        has_blocking = any(i.get("severity") == "blocking" for i in issues)
        if verdict == "pass" and has_blocking:
            return ToolResult(text="verdict=pass 但存在 blocking 意见——pass 当且仅当无 blocking")
        if verdict == "fail" and not has_blocking:
            return ToolResult(text="verdict=fail 但无 blocking 意见——fail 必须含至少一个 blocking")
        # ④ 格式化：verdict 行 + 按 severity 分组 issue 清单（generate-note 确定性可读）
        lines = [f"审查裁决：{verdict}"]
        for sev in SEVERITIES:
            for issue in issues:
                if issue["severity"] == sev:
                    lines.append(f"- [{sev.upper()}] {issue['dimension']} | {issue['location']} | {issue['action']}")
        return ToolResult(text="\n".join(lines))
