from paperflow.tools.search import DedupPapersTool, FilterPapersTool


def test_dedup_by_arxiv_id():
    tool = DedupPapersTool()
    papers = [
        {"title": "Paper A", "arxiv_id": "2101.00001"},
        {"title": "Paper A (same)", "arxiv_id": "2101.00001"},
        {"title": "Paper B", "arxiv_id": "2101.00002"},
    ]
    result = tool.execute(papers=papers)
    assert "Paper B" in result.text
    assert result.text.count("2101.00001") == 1     # 同一 arXiv ID 只留一条


def test_dedup_by_normalized_title():
    tool = DedupPapersTool()
    papers = [
        {"title": "Graph Neural Networks."},
        {"title": "graph neural networks"},
    ]
    result = tool.execute(papers=papers)
    assert result.text.count("Graph Neural") <= 1    # 规范化标题去重


def test_filter_by_year_and_citations():
    tool = FilterPapersTool()
    papers = [
        {"title": "A", "year": 2020, "cited_by_count": 5},
        {"title": "B", "year": 2023, "cited_by_count": 50},
    ]
    result = tool.execute(papers=papers, year_min=2022, min_citations=10)
    assert "B" in result.text
    assert "A" not in result.text
