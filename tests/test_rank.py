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


def test_recency_and_community_synergy():
    from datetime import date, timedelta
    from infoseek.rank import _recency_bonus, _engagement_bonus

    recent_date = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    old_date = (date.today() - timedelta(days=120)).strftime("%Y-%m-%d")

    r_fresh = Result("Fresh Post", "https://reddit.com/r/test", source="reddit", date=recent_date, extra="50 points, 20 comments")
    r_old = Result("Old Post", "https://reddit.com/r/test2", source="reddit", date=old_date, extra="50 points, 20 comments")

    assert _recency_bonus(r_fresh) > _recency_bonus(r_old)
    assert _engagement_bonus(r_fresh) > _engagement_bonus(r_old)  # synergy bonus triggered for fresh community post


def test_to_dicts_prepends_community_extra():
    from infoseek.rank import to_dicts

    res = [
        Result("Title 1", "https://reddit.com/r/python", snippet="Great library!", source="reddit", extra="r/python · 35 upvotes"),
        Result("Title 2", "https://docs.python.org", snippet="Standard docs", source="bing", extra=""),
    ]
    dicts = to_dicts(res)
    assert dicts[0]["snippet"].startswith("[r/python · 35 upvotes]")
    assert dicts[1]["snippet"] == "Standard docs"


def test_clean_default_limit_240():
    text = "word " * 100
    cleaned = clean(text)
    assert len(cleaned) <= 245
    assert len(cleaned) > 200


def test_to_dicts_pruning_and_score_rounding():
    from infoseek.rank import to_dicts
    res = [
        Result("Title", "https://example.com", snippet="Hi", source="bing", score=9.87654),
        Result("Social", "https://reddit.com", snippet="Yo", source="reddit", score=12.1, upvotes=15, comments=3),
    ]
    unpruned = to_dicts(res, prune=False)
    assert unpruned[0]["score"] == 9.88
    assert "extra" in unpruned[0]

    pruned = to_dicts(res, prune=True)
    assert pruned[0]["score"] == 9.88
    assert "extra" not in pruned[0]
    assert "date" not in pruned[0]
    assert pruned[1]["upvotes"] == 15
    assert pruned[1]["comments"] == 3

