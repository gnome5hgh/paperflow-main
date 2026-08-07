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
