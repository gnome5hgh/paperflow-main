import pytest
from paperflow.tools.submit_download_review import SubmitDownloadReviewTool

def _item(decision="pass", reasons=None, **kw):
    d = {"title": "Paper X", "venue_rank": {"ccf": "A"}, "decision": decision,
         "reasons": reasons if reasons is not None else ["等级通过"], "source_link": "https://x"}
    d.update(kw)
    return d

def test_valid_verdict_formats():
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="pass", items=[_item()])
    assert "审查裁决：pass" in r.text and "Paper X" in r.text

def test_verdict_enum_validation():
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="maybe", items=[_item()])
    assert "verdict 非法" in r.text

def test_decision_enum_validation():
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="pass", items=[_item(decision="skip")])
    assert "decision 非法" in r.text

def test_fail_item_requires_reasons():
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="fail", items=[_item(decision="fail", reasons=[])])
    assert "reasons" in r.text

def test_verdict_consistency():
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="pass", items=[_item(decision="fail")])
    assert "一致" in r.text

def test_verdict_fail_with_pass_item_rejected():
    # 对称方向：verdict=fail 却含 pass 条目 → 输出"审查裁决：fail"却带 [PASS] 行，自相矛盾，必须拦截
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="fail", items=[_item(decision="pass")])
    assert "一致" in r.text

def test_verdict_fail_empty_items_valid():
    # fail = 无任何合格项，空清单正是其极端情况，必须保持合法（不拦截、正常格式化）
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="fail", items=[])
    assert "审查裁决：fail" in r.text

def test_verdict_pass_empty_items_rejected():
    # pass 语义 = 存在可下载/推荐项，空清单自相矛盾（过宽放行）——必须拦截，
    # 与 fail+空 的合法极端形成对称。LLM 把"没有合格项"误报成 pass 时此处兜住。
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="pass", items=[])
    assert "空" in r.text
