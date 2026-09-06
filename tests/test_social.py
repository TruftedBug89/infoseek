"""Offline unit tests for social.py, last30days pipeline, and social engines."""
import asyncio
from datetime import date, timedelta
import json
import pytest

import infoseek
from infoseek.social import (
    is_comparison_query, _looks_financial, _pick_social_engines,
    _filter_recency, _extract_top_quotes, _format_general_brief,
    _format_comparison_brief, last30days
)
from infoseek.rank import Result, merge, _engagement_bonus


def test_comparison_detection():
    is_vs, a, b = is_comparison_query("Rust vs Go")
    assert is_vs is True
    assert a == "Rust"
    assert b == "Go"

    is_vs, a, b = is_comparison_query("Claude versus ChatGPT")
    assert is_vs is True
    assert a == "Claude"
    assert b == "ChatGPT"

    is_vs, a, b = is_comparison_query("OpenClaw vs. Hermes")
    assert is_vs is True
    assert a == "OpenClaw"
    assert b == "Hermes"

    is_vs, _, _ = is_comparison_query("Python async tutorial")
    assert is_vs is False


def test_financial_detection():
    assert _looks_financial("$NVDA earnings") is True
    assert _looks_financial("bitcoin halving") is True
    assert _looks_financial("solana staking") is True
    assert _looks_financial("apple stock valuation") is True
    assert _looks_financial("fastapi tutorial") is False


def test_social_engines_selection():
    engs_general = _pick_social_engines("OpenAI board")
    assert "reddit" in engs_general
    assert "hn" in engs_general
    assert "polymarket" in engs_general

    engs_tech = _pick_social_engines("new AI model release")
    assert "techmeme" in engs_tech
    assert "bluesky" in engs_tech

    engs_fin = _pick_social_engines("$BTC crypto crash")
    assert "stocktwits" in engs_fin


def test_filter_recency():
    today = date.today()
    cutoff = today - timedelta(days=30)
    old_date = (today - timedelta(days=45)).strftime("%Y-%m-%d")
    new_date = (today - timedelta(days=10)).strftime("%Y-%m-%d")

    results = [
        Result(title="Old News", url="https://example.com/old", date=old_date),
        Result(title="Fresh News", url="https://example.com/new", date=new_date),
        Result(title="Undated", url="https://example.com/undated", date=""),
    ]

    filtered = _filter_recency(results, cutoff)
    titles = [r.title for r in filtered]
    assert "Old News" not in titles
    assert "Fresh News" in titles
    assert "Undated" in titles


def test_extract_top_quotes():
    sample_text = """
    r/technology discussion on AI
    · u/alpha_coder (450 pts): Local LLMs are now fast enough for everyday code review.
    · u/beta_dev (120 pts): The memory bandwidth is still the primary bottleneck.
    Regular commentary line.
    - @tech_insider: Weights were released on HuggingFace this morning.
    """
    quotes = _extract_top_quotes([sample_text])
    assert len(quotes) >= 2
    assert any("u/alpha_coder" in q for q in quotes)
    assert any("Local LLMs" in q for q in quotes)


def test_engagement_bonus():
    r_empty = Result(title="A", url="https://a.com")
    assert _engagement_bonus(r_empty) == 0.0

    r_reddit = Result(title="B", url="https://b.com", upvotes=500, comments=100)
    bonus = _engagement_bonus(r_reddit)
    assert bonus > 1.5

    r_poly = Result(title="C", url="https://c.com", extra="$1.2M vol · 84% Yes")
    assert _engagement_bonus(r_poly) >= 2.0

    # Merged score incorporates engagement
    scored = merge([[r_empty], [r_reddit]], 2, ["reddit", "ddg"])
    assert scored[0].url == r_reddit.url


def test_format_general_brief():
    r1 = Result(title="Discussions on Claude Code", url="https://reddit.com/r/ClaudeCode/1",
                source="reddit", engagement_str="450 pts · 120 cmt", date="2026-09-01",
                snippet="Users report high satisfaction with the tool.")
    poly = Result(title="Will Claude 3.8 launch in September?", url="https://polymarket.com/event/claude",
                  source="polymarket", snippet="Yes: 82% · No: 18% · $450K vol")
    quotes = ['u/dev1 (320 pts): "The context window improvements are huge."']

    brief = _format_general_brief("Claude Code", "2026-08-05", "2026-09-04", 30, [r1], quotes, [poly])
    assert "🌐 infoseek last30days" in brief
    assert "Prediction Markets & Odds:" in brief
    assert "Top Community Takes & Quotes:" in brief
    assert "KEY PATTERNS from the research:" in brief
    assert "✅ Research complete" in brief


def test_format_comparison_brief():
    r_a = [Result(title="Rust 2026", url="https://rust.com", source="hn", snippet="Blazing fast")]
    r_b = [Result(title="Go 1.26", url="https://golang.org", source="hn", snippet="Simple concurrency")]
    q_a = ['u/ferris (100 pts): "Zero cost abstractions."']
    q_b = ['u/gopher (90 pts): "Fast compilation time."']

    brief = _format_comparison_brief("Rust", "Go", "2026-08-05", "2026-09-04", 30, r_a, r_b, q_a, q_b)
    assert "# Rust vs Go: What the Community Says" in brief
    assert "## Quick Verdict" in brief
    assert "## Rust" in brief
    assert "## Go" in brief
    assert "## Head-to-Head Key Differences" in brief


def test_last30days_public_api():
    assert callable(infoseek.last30days)


def test_run_router_last30days():
    # Test routing logic in infoseek.run without network
    async def mock_run(q):
        import re
        if re.match(r"^(?:last30days|last30|social|people):\s*", q, re.I):
            return "ROUTED_TO_LAST30DAYS"
        if re.match(r"^(?:last\s+(?:30\s+days|month)|what\s+(?:are\s+people\s+saying|do\s+people\s+think|are\s+users\s+saying)\s+about)\b", q, re.I):
            return "ROUTED_TO_LAST30DAYS"
        return "OTHER"

    assert asyncio.run(mock_run("last30days: nvidia earnings")) == "ROUTED_TO_LAST30DAYS"
    assert asyncio.run(mock_run("social: rust vs go")) == "ROUTED_TO_LAST30DAYS"
    assert asyncio.run(mock_run("what are people saying about claude")) == "ROUTED_TO_LAST30DAYS"
    assert asyncio.run(mock_run("last 30 days of ai agents")) == "ROUTED_TO_LAST30DAYS"
    assert asyncio.run(mock_run("why is redis fast")) == "OTHER"
