# paperflow/tools/review/submit_review.py
"""SubmitReviewTool：汇总对一篇笔记的审查裁决(reviewer 笔记审查模式返回)。

把审查结果规范成结构化裁决(verdict + issues[],各字段枚举校验)。execute 不读文件
内容,只把提交的字段格式化,因此放开 scratch 根零安全影响。校验哲学对齐 edit_file
的 miss/multi 报错:非法输入返回可行动报错文本,不静默吞——让 reviewer 的 LLM
修正后重试。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.review._validate import VERDICTS, enum_check

#: severity 合法值(blocking 必须修才能通过;major 应修;minor 可忽略)
SEVERITIES = ("blocking", "major", "minor")
#: dimension 合法值(5 个审查维度;不设"写作质量"维度,由其余维度覆盖)
DIMENSIONS = ("requirements", "faithfulness", "consistency", "completeness", "structure")


class SubmitReviewTool(Tool):
    name = "submit_review"
    description = ("汇总对一篇笔记的审查裁决（reviewer 笔记审查模式返回）。"
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
    allowed_roots = ["note", "scratch", "outline"]

    def execute(self, path: str, verdict: str, issues: list) -> ToolResult:
        """校验并格式化审查裁决;非法输入返回可行动报错文本。

        三步校验:verdict 枚举 → 逐 issue 枚举/必需字段 → verdict 与 issues 一致性
        (pass 当且仅当无 blocking)。通过后按 severity 分组渲染,供 writer 确定性读取。
        """
        # ① verdict 枚举校验（enum_check 共享，同 submit_download_review）
        bad = enum_check(verdict, VERDICTS, "verdict")
        if bad:
            return ToolResult(text=bad)
        # ② 逐 issue 校验：severity/dimension 枚举 + location/action 非空
        for issue in issues:
            bad = enum_check(issue.get("severity"), SEVERITIES, "severity")
            if bad:
                return ToolResult(text=bad)
            bad = enum_check(issue.get("dimension"), DIMENSIONS, "dimension")
            if bad:
                return ToolResult(text=bad)
            missing = [k for k in ("location", "action") if not issue.get(k)]
            if missing:
                return ToolResult(text=f"issue 缺少字段: {', '.join(missing)}——每条意见必须有 location + action")
        # ③ verdict 与 issues 一致性（pass 语义被破坏则报错，给 LLM 可行动指引）
        has_blocking = any(i.get("severity") == "blocking" for i in issues)
        if verdict == "pass" and has_blocking:
            return ToolResult(text="verdict=pass 但存在 blocking 意见——pass 当且仅当无 blocking")
        if verdict == "fail" and not has_blocking:
            return ToolResult(text="verdict=fail 但无 blocking 意见——fail 必须含至少一个 blocking")
        # ④ 格式化：verdict 行 + 按 severity 分组 issue 清单（writer 确定性可读）
        lines = [f"审查裁决：{verdict}"]
        for sev in SEVERITIES:
            for issue in issues:
                if issue["severity"] == sev:
                    lines.append(f"- [{sev.upper()}] {issue['dimension']} | {issue['location']} | {issue['action']}")
        return ToolResult(text="\n".join(lines))
