import time
from paperflow.tools.search._common import (
    SearchRunState, get_run_state, query_cache_get, query_cache_put,
    breaker_is_open, breaker_register_failure, breaker_register_success,
    QUERY_CACHE_MAX,
)

def _p(title, **kw):
    d = {"title": title, "arxiv_id": None, "doi": None}
    d.update(kw)
    return d

def test_pool_dedups_by_doi_then_arxiv_then_title():
    st = SearchRunState()
    added = st.add([_p("A", doi="10.1/a"), _p("A", doi="10.1/a"),   # doi 重复
                    _p("B", arxiv_id="2101.00001"), _p("B", arxiv_id="2101.00001")])
    assert len(added) == 2
    assert len(st.as_candidates()) == 2
    # 同论文跨源合并：arxiv 条目无 doi，openalex 条目带 doi → 两键不同不合并，但同源去重已生效
    # 规范化标题兜底
    st.add([_p("C"), _p("c!")])
    assert len(st.as_candidates()) == 3

def test_pool_merges_missing_source_fields():
    st = SearchRunState()
    st.add([_p("D", arxiv_id="1", pdf_url="")])
    st.add([_p("D", arxiv_id="1", pdf_url="http://x/pdf")])
    cands = st.as_candidates()
    assert cands[0]["pdf_url"] == "http://x/pdf"

def test_get_run_state_isolated_by_trace_id():
    a, b = get_run_state("t1"), get_run_state("t2")
    a.add([_p("X")])
    assert get_run_state("t1").as_candidates() != []
    assert get_run_state("t2").as_candidates() == []

def test_query_cache_lru_hit_and_evict():
    for i in range(QUERY_CACHE_MAX + 5):
        query_cache_put(("q", i), f"res{i}")
    assert query_cache_get(("q", 0)) is None          # 最旧被逐出
    assert query_cache_get(("q", QUERY_CACHE_MAX + 4)) == f"res{QUERY_CACHE_MAX + 4}"

def test_breaker_opens_after_two_failures_and_recovers():
    assert not breaker_is_open("arxiv")
    breaker_register_failure("arxiv"); assert not breaker_is_open("arxiv")
    breaker_register_failure("arxiv"); assert breaker_is_open("arxiv")
    breaker_register_success("arxiv"); assert not breaker_is_open("arxiv")
