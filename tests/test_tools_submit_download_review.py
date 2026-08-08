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
    """pass 语义=存在可下载/推荐项：verdict=pass 但无任何 pass 条目 → 自相矛盾，必须拦截。

    Fix 2 后按「pass 项存在性」判定（旧语义按 fail 项拦截，误拒了 pass+混合 的合法形态）。
    单个 fail 条目 = 无 pass 条目 → 仍拒绝；判定词"不一致"在报错文案里。"""
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="pass", items=[_item(decision="fail")])
    assert "不一致" in r.text

def test_verdict_pass_allows_mixed_items():
    """Fix 2 根因回归：pass + 混合列表合法（2 pass + 1 fail + verdict=pass → 通过）。

    真实审查产出「2 pass + 7 fail + verdict=pass」被旧校验（verdict=pass 且存在 fail 项
    即拒）逼着试错——pass 语义只要求存在可下载/推荐项，剩余 fail 项只是不值得下载的
    候选。断言正常格式化且 PASS/FAIL 标签数量正确。"""
    tool = SubmitDownloadReviewTool()
    r = tool.execute(verdict="pass", items=[
        _item(title="Paper A"),
        _item(title="Paper B"),
        _item(title="Paper C", decision="fail", reasons=["等级不够"]),
    ])
    assert "审查裁决：pass" in r.text
    assert r.text.count("[PASS]") == 2 and r.text.count("[FAIL]") == 1

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
