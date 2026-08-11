"""Offline tests for relevance-based extraction."""
from infoseek.extract import relevant_sentences

TEXT = (
    "This page is about cooking pasta. The weather was nice on Tuesday. "
    "Best pasta recipes use semolina flour and salt. Nothing else here matters at all. "
    "Simmer the sauce for twenty minutes. Pasta water should be salted generously."
)


def test_relevant_sentences_keep_query_terms():
    sel = relevant_sentences(TEXT, "pasta recipes", 500)
    assert "pasta" in sel and "weather" not in sel
    assert "Best pasta recipes" in sel


def test_relevant_sentences_respect_budget():
    sel = relevant_sentences(TEXT, "pasta", 80)
    assert len(sel) <= 120  # budget + slack for sentence granularity
