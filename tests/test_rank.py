"""Offline tests for normalization, dedupe, and scored merging."""
from infoseek.rank import Result, clean, dedupe, merge, normalize_url


def test_normalize_url_strips_tracking_and_www():
    assert normalize_url("https://www.example.com/a?utm_source=x&b=1#frag") == "https://example.com/a?b=1"
    assert normalize_url("https://m.example.com/x") == "https://example.com/x"


def test_dedupe_near_duplicate_titles():
    dup = [
        Result("Same Article - SiteA", "https://a.com/x"),
        Result("Same Article | SiteB", "https://www.a.com/x"),
        Result("Unique", "https://b.com/y"),
    ]
    assert len(dedupe(dup)) == 2


def test_merge_priority_and_diversity_cap():
    m1 = [Result(f"ddg{i}", f"https://ddg{i}.com", source="ddg", rank=i) for i in range(5)]
    m2 = [Result(f"news{i}", f"https://news{i}.com", source="news", rank=i) for i in range(5)]
    merged = merge([m1, m2], 6, ["ddg", "news"])
    assert merged[0].source == "ddg"
    assert len(merged) == 6
    assert sum(1 for x in merged if x.source == "ddg") <= 3  # diversity cap


def test_clean_removes_cta_boilerplate():
    out = clean("Here is a long sentence about things. Discover more about it now", 160)
    assert "Discover" not in out and "things" in out


def test_clean_truncates():
    out = clean("word " * 100, 160)
    assert len(out) <= 165
