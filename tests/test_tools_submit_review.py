# tests/test_tools_submit_review.py
"""SubmitReviewTool 单元测试：枚举校验 + verdict/issue 一致性 + 格式化输出。"""
from paperflow.tools.submit_review import SubmitReviewTool


def _tool():
    return SubmitReviewTool()


def test_submit_review_pass_no_issues():
    result = _tool().execute(path="/x/a.md", verdict="pass", issues=[])
    assert "审查裁决：pass" in result.text


def test_submit_review_fail_with_blocking():
    issues = [{"severity": "blocking", "dimension": "requirements",
               "location": "概述", "action": "压缩到 500 字"}]
    result = _tool().execute(path="/x/a.md", verdict="fail", issues=issues)
    assert "审查裁决：fail" in result.text
    assert "[BLOCKING] requirements | 概述 | 压缩到 500 字" in result.text


def test_submit_review_groups_by_severity():
    issues = [
        {"severity": "minor", "dimension": "completeness", "location": "相关工作", "action": "补一句"},
        {"severity": "blocking", "dimension": "faithfulness", "location": "方法", "action": "数字对齐原文"},
    ]
    result = _tool().execute(path="/x/a.md", verdict="fail", issues=issues)
    assert result.text.index("[BLOCKING]") < result.text.index("[MINOR]")


def test_submit_review_rejects_invalid_severity():
    issues = [{"severity": "critical", "dimension": "faithfulness", "location": "方法", "action": "x"}]
    result = _tool().execute(path="/x/a.md", verdict="fail", issues=issues)
    assert "severity 非法" in result.text and "critical" in result.text


def test_submit_review_rejects_invalid_dimension():
    issues = [{"severity": "major", "dimension": "quality", "location": "概述", "action": "x"}]
    result = _tool().execute(path="/x/a.md", verdict="fail", issues=issues)
    assert "dimension 非法" in result.text and "quality" in result.text


def test_submit_review_rejects_missing_location_action():
    issues = [{"severity": "major", "dimension": "completeness", "location": "", "action": ""}]
    result = _tool().execute(path="/x/a.md", verdict="fail", issues=issues)
    assert "缺少字段" in result.text and "location" in result.text


def test_submit_review_pass_with_blocking_rejected():
    issues = [{"severity": "blocking", "dimension": "structure", "location": "概述", "action": "x"}]
    result = _tool().execute(path="/x/a.md", verdict="pass", issues=issues)
    assert "pass 当且仅当无 blocking" in result.text


def test_submit_review_fail_without_blocking_rejected():
    issues = [{"severity": "major", "dimension": "completeness", "location": "概述", "action": "x"}]
    result = _tool().execute(path="/x/a.md", verdict="fail", issues=issues)
    assert "fail 必须含至少一个 blocking" in result.text


def test_submit_review_meta():
    tool = _tool()
    assert tool.name == "submit_review"
    assert tool.risk_level == "low"
    assert tool.allowed_roots == ["note", "scratch"]
